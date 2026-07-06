WITH bounds AS (
  SELECT
    now() AS generated_at,
    date_trunc('day', now()) AS today_start,
    now() - interval '7 days' AS last_7d_start,
    now() - interval '30 days' AS last_30d_start,
    date_trunc('hour', now()) - interval '47 hours' AS hourly_start
),
ranges AS (
  SELECT 'today'::text AS label, today_start AS start_at FROM bounds
  UNION ALL
  SELECT 'last_7d'::text AS label, last_7d_start AS start_at FROM bounds
  UNION ALL
  SELECT 'last_30d'::text AS label, last_30d_start AS start_at FROM bounds
),
logs_30d AS MATERIALIZED (
  SELECT ul.*
  FROM usage_logs ul
  CROSS JOIN bounds b
  WHERE ul.created_at >= b.last_30d_start
),
range_rollups AS (
  SELECT
    r.label,
    count(ul.id)::bigint AS requests,
    coalesce(sum(ul.input_tokens), 0)::bigint AS input_tokens,
    coalesce(sum(ul.output_tokens), 0)::bigint AS output_tokens,
    coalesce(sum(ul.cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
    coalesce(sum(ul.cache_read_tokens), 0)::bigint AS cache_read_tokens,
    coalesce(sum(ul.image_output_tokens), 0)::bigint AS image_output_tokens,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
    coalesce(round(sum(ul.actual_cost)::numeric, 6), 0) AS actual_cost,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost))::numeric, 6), 0) AS account_cost,
    coalesce(round(avg(ul.duration_ms) FILTER (WHERE ul.duration_ms IS NOT NULL)::numeric, 0), 0) AS avg_duration_ms,
    coalesce(round((percentile_cont(0.95) WITHIN GROUP (ORDER BY ul.duration_ms) FILTER (WHERE ul.duration_ms IS NOT NULL))::numeric, 0), 0) AS p95_duration_ms,
    count(DISTINCT ul.user_id)::bigint AS active_users,
    count(DISTINCT ul.account_id)::bigint AS accounts_used
  FROM ranges r
  LEFT JOIN logs_30d ul ON ul.created_at >= r.start_at
  GROUP BY r.label
),
range_json AS (
  SELECT
    label,
    jsonb_build_object(
      'requests', requests,
      'input_tokens', input_tokens,
      'output_tokens', output_tokens,
      'cache_creation_tokens', cache_creation_tokens,
      'cache_read_tokens', cache_read_tokens,
      'image_output_tokens', image_output_tokens,
      'total_tokens', total_tokens,
      'total_cost', total_cost,
      'actual_cost', actual_cost,
      'account_cost', account_cost,
      'avg_duration_ms', avg_duration_ms,
      'p95_duration_ms', p95_duration_ms,
      'active_users', active_users,
      'accounts_used', accounts_used
    ) AS payload
  FROM range_rollups
),
account_usage AS (
  SELECT
    ul.account_id AS id,
    count(ul.id) FILTER (WHERE ul.created_at >= b.today_start)::bigint AS today_requests,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens) FILTER (WHERE ul.created_at >= b.today_start), 0)::bigint AS today_tokens,
    coalesce(round(sum(ul.total_cost) FILTER (WHERE ul.created_at >= b.today_start)::numeric, 6), 0) AS today_total_cost,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost)) FILTER (WHERE ul.created_at >= b.today_start)::numeric, 6), 0) AS today_account_cost,
    count(ul.id) FILTER (WHERE ul.created_at >= b.last_7d_start)::bigint AS requests_7d,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens) FILTER (WHERE ul.created_at >= b.last_7d_start), 0)::bigint AS tokens_7d,
    coalesce(round(sum(ul.total_cost) FILTER (WHERE ul.created_at >= b.last_7d_start)::numeric, 6), 0) AS total_cost_7d,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost)) FILTER (WHERE ul.created_at >= b.last_7d_start)::numeric, 6), 0) AS account_cost_7d,
    count(ul.id)::bigint AS requests_30d,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS tokens_30d,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost_30d,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost))::numeric, 6), 0) AS account_cost_30d,
    coalesce(round(avg(ul.duration_ms) FILTER (WHERE ul.duration_ms IS NOT NULL)::numeric, 0), 0) AS avg_duration_ms_30d,
    max(ul.created_at) AS last_request_at
  FROM logs_30d ul
  CROSS JOIN bounds b
  GROUP BY ul.account_id
),
account_rollups AS (
  SELECT
    a.id,
    coalesce(au.today_requests, 0)::bigint AS today_requests,
    coalesce(au.today_tokens, 0)::bigint AS today_tokens,
    coalesce(au.today_total_cost, 0) AS today_total_cost,
    coalesce(au.today_account_cost, 0) AS today_account_cost,
    coalesce(au.requests_7d, 0)::bigint AS requests_7d,
    coalesce(au.tokens_7d, 0)::bigint AS tokens_7d,
    coalesce(au.total_cost_7d, 0) AS total_cost_7d,
    coalesce(au.account_cost_7d, 0) AS account_cost_7d,
    coalesce(au.requests_30d, 0)::bigint AS requests_30d,
    coalesce(au.tokens_30d, 0)::bigint AS tokens_30d,
    coalesce(au.total_cost_30d, 0) AS total_cost_30d,
    coalesce(au.account_cost_30d, 0) AS account_cost_30d,
    coalesce(au.avg_duration_ms_30d, 0) AS avg_duration_ms_30d,
    au.last_request_at
  FROM accounts a
  LEFT JOIN account_usage au ON au.id = a.id
  WHERE a.deleted_at IS NULL
),
api_key_counts AS (
  SELECT
    user_id,
    count(*)::bigint AS api_key_count,
    count(*) FILTER (WHERE status = 'active')::bigint AS active_api_key_count,
    max(last_used_at) AS last_key_used_at
  FROM api_keys
  WHERE deleted_at IS NULL
  GROUP BY user_id
),
subscription_counts AS (
  SELECT
    user_id,
    count(*) FILTER (WHERE status = 'active')::bigint AS active_subscription_count,
    max(expires_at) FILTER (WHERE status = 'active') AS active_subscription_expires_at,
    coalesce(round(sum(monthly_usage_usd) FILTER (WHERE status = 'active')::numeric, 6), 0) AS active_monthly_usage_usd
  FROM user_subscriptions
  WHERE deleted_at IS NULL
  GROUP BY user_id
),
user_usage AS (
  SELECT
    ul.user_id AS id,
    count(ul.id) FILTER (WHERE ul.created_at >= b.today_start)::bigint AS today_requests,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens) FILTER (WHERE ul.created_at >= b.today_start), 0)::bigint AS today_tokens,
    coalesce(round(sum(ul.total_cost) FILTER (WHERE ul.created_at >= b.today_start)::numeric, 6), 0) AS today_total_cost,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost)) FILTER (WHERE ul.created_at >= b.today_start)::numeric, 6), 0) AS today_account_cost,
    count(ul.id) FILTER (WHERE ul.created_at >= b.last_7d_start)::bigint AS requests_7d,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens) FILTER (WHERE ul.created_at >= b.last_7d_start), 0)::bigint AS tokens_7d,
    coalesce(round(sum(ul.total_cost) FILTER (WHERE ul.created_at >= b.last_7d_start)::numeric, 6), 0) AS total_cost_7d,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost)) FILTER (WHERE ul.created_at >= b.last_7d_start)::numeric, 6), 0) AS account_cost_7d,
    count(ul.id)::bigint AS requests_30d,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS tokens_30d,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost_30d,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost))::numeric, 6), 0) AS account_cost_30d,
    coalesce(round(avg(ul.duration_ms) FILTER (WHERE ul.duration_ms IS NOT NULL)::numeric, 0), 0) AS avg_duration_ms_30d,
    count(DISTINCT ul.account_id)::bigint AS accounts_used_30d,
    max(ul.created_at) AS last_request_at
  FROM logs_30d ul
  CROSS JOIN bounds b
  GROUP BY ul.user_id
),
user_rollups AS (
  SELECT
    u.id,
    coalesce(uu.today_requests, 0)::bigint AS today_requests,
    coalesce(uu.today_tokens, 0)::bigint AS today_tokens,
    coalesce(uu.today_total_cost, 0) AS today_total_cost,
    coalesce(uu.today_account_cost, 0) AS today_account_cost,
    coalesce(uu.requests_7d, 0)::bigint AS requests_7d,
    coalesce(uu.tokens_7d, 0)::bigint AS tokens_7d,
    coalesce(uu.total_cost_7d, 0) AS total_cost_7d,
    coalesce(uu.account_cost_7d, 0) AS account_cost_7d,
    coalesce(uu.requests_30d, 0)::bigint AS requests_30d,
    coalesce(uu.tokens_30d, 0)::bigint AS tokens_30d,
    coalesce(uu.total_cost_30d, 0) AS total_cost_30d,
    coalesce(uu.account_cost_30d, 0) AS account_cost_30d,
    coalesce(uu.avg_duration_ms_30d, 0) AS avg_duration_ms_30d,
    coalesce(uu.accounts_used_30d, 0)::bigint AS accounts_used_30d,
    uu.last_request_at
  FROM users u
  LEFT JOIN user_usage uu ON uu.id = u.id
  WHERE u.deleted_at IS NULL
),
account_model_rollups AS (
  SELECT
    ul.account_id,
    ul.model,
    count(*)::bigint AS requests,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens
  FROM logs_30d ul
  CROSS JOIN bounds b
  WHERE ul.created_at >= b.last_7d_start
  GROUP BY ul.account_id, ul.model
),
account_model_ranked AS (
  SELECT
    *,
    row_number() OVER (PARTITION BY account_id ORDER BY requests DESC, total_cost DESC, model) AS rank
  FROM account_model_rollups
),
user_model_rollups AS (
  SELECT
    ul.user_id,
    ul.model,
    count(*)::bigint AS requests,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens
  FROM logs_30d ul
  CROSS JOIN bounds b
  WHERE ul.created_at >= b.last_7d_start
  GROUP BY ul.user_id, ul.model
),
user_model_ranked AS (
  SELECT
    *,
    row_number() OVER (PARTITION BY user_id ORDER BY requests DESC, total_cost DESC, model) AS rank
  FROM user_model_rollups
),
daily_series AS (
  SELECT generate_series(
    (SELECT date_trunc('day', generated_at) - interval '29 days' FROM bounds),
    (SELECT date_trunc('day', generated_at) FROM bounds),
    interval '1 day'
  ) AS bucket_start
),
daily_usage AS (
  SELECT
    date_trunc('day', ul.created_at) AS bucket_start,
    count(ul.id)::bigint AS requests,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
    coalesce(round(sum(coalesce(ul.account_stats_cost, ul.actual_cost))::numeric, 6), 0) AS account_cost,
    count(DISTINCT ul.user_id)::bigint AS active_users,
    count(DISTINCT ul.account_id)::bigint AS accounts_used
  FROM logs_30d ul
  GROUP BY date_trunc('day', ul.created_at)
),
daily_rollups AS (
  SELECT
    ds.bucket_start,
    coalesce(du.requests, 0)::bigint AS requests,
    coalesce(du.total_tokens, 0)::bigint AS total_tokens,
    coalesce(du.total_cost, 0) AS total_cost,
    coalesce(du.account_cost, 0) AS account_cost,
    coalesce(du.active_users, 0)::bigint AS active_users,
    coalesce(du.accounts_used, 0)::bigint AS accounts_used
  FROM daily_series ds
  LEFT JOIN daily_usage du ON du.bucket_start = ds.bucket_start
),
hourly_series AS (
  SELECT generate_series(
    (SELECT hourly_start FROM bounds),
    (SELECT date_trunc('hour', generated_at) FROM bounds),
    interval '1 hour'
  ) AS bucket_start
),
hourly_usage AS (
  SELECT
    date_trunc('hour', ul.created_at) AS bucket_start,
    count(ul.id)::bigint AS requests,
    coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens,
    coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
    count(DISTINCT ul.user_id)::bigint AS active_users
  FROM logs_30d ul
  CROSS JOIN bounds b
  WHERE ul.created_at >= b.hourly_start
  GROUP BY date_trunc('hour', ul.created_at)
),
hourly_rollups AS (
  SELECT
    hs.bucket_start,
    coalesce(hu.requests, 0)::bigint AS requests,
    coalesce(hu.total_tokens, 0)::bigint AS total_tokens,
    coalesce(hu.total_cost, 0) AS total_cost,
    coalesce(hu.active_users, 0)::bigint AS active_users
  FROM hourly_series hs
  LEFT JOIN hourly_usage hu ON hu.bucket_start = hs.bucket_start
)
SELECT jsonb_pretty(jsonb_build_object(
  'generated_at', (SELECT generated_at FROM bounds),
  'server_timezone', current_setting('TIMEZONE'),
  'source', jsonb_build_object(
    'logs_min_at', (SELECT min(created_at) FROM usage_logs),
    'logs_max_at', (SELECT max(created_at) FROM usage_logs),
    'usage_log_count', (SELECT count(*) FROM usage_logs)
  ),
  'totals', jsonb_build_object(
    'users', (SELECT count(*) FROM users WHERE deleted_at IS NULL),
    'active_users_total', (SELECT count(*) FROM users WHERE deleted_at IS NULL AND status = 'active'),
    'accounts', (SELECT count(*) FROM accounts WHERE deleted_at IS NULL),
    'active_accounts', (SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND status = 'active'),
    'schedulable_accounts', (SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND schedulable = true),
    'error_accounts', (SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND status <> 'active'),
    'api_keys', (SELECT count(*) FROM api_keys WHERE deleted_at IS NULL),
    'active_api_keys', (SELECT count(*) FROM api_keys WHERE deleted_at IS NULL AND status = 'active'),
    'today', (SELECT payload FROM range_json WHERE label = 'today'),
    'last_7d', (SELECT payload FROM range_json WHERE label = 'last_7d'),
    'last_30d', (SELECT payload FROM range_json WHERE label = 'last_30d')
  ),
  'accounts', coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'id', a.id,
      'name', a.name,
      'platform', a.platform,
      'type', a.type,
      'status', a.status,
      'schedulable', a.schedulable,
      'priority', a.priority,
      'concurrency', a.concurrency,
      'quota_dimension', a.quota_dimension,
      'load_factor', a.load_factor,
      'error_message', nullif(a.error_message, ''),
      'last_used_at', a.last_used_at,
      'last_request_at', ar.last_request_at,
      'created_at', a.created_at,
      'updated_at', a.updated_at,
      'rate_limited_at', a.rate_limited_at,
      'rate_limit_reset_at', a.rate_limit_reset_at,
      'overload_until', a.overload_until,
      'session_window_start', a.session_window_start,
      'session_window_end', a.session_window_end,
      'session_window_status', a.session_window_status,
      'temp_unschedulable_until', a.temp_unschedulable_until,
      'temp_unschedulable_reason', nullif(a.temp_unschedulable_reason, ''),
      'expires_at', a.expires_at,
      'codex_usage', jsonb_build_object(
        'updated_at', nullif(a.extra->>'codex_usage_updated_at', ''),
        'window_5h', jsonb_build_object(
          'used_percent', nullif(a.extra->>'codex_5h_used_percent', '')::numeric,
          'reset_at', nullif(a.extra->>'codex_5h_reset_at', ''),
          'reset_after_seconds', nullif(a.extra->>'codex_5h_reset_after_seconds', '')::numeric,
          'window_minutes', nullif(a.extra->>'codex_5h_window_minutes', '')::numeric
        ),
        'window_7d', jsonb_build_object(
          'used_percent', nullif(a.extra->>'codex_7d_used_percent', '')::numeric,
          'reset_at', nullif(a.extra->>'codex_7d_reset_at', ''),
          'reset_after_seconds', nullif(a.extra->>'codex_7d_reset_after_seconds', '')::numeric,
          'window_minutes', nullif(a.extra->>'codex_7d_window_minutes', '')::numeric
        ),
        'primary', jsonb_build_object(
          'used_percent', nullif(a.extra->>'codex_primary_used_percent', '')::numeric,
          'reset_after_seconds', nullif(a.extra->>'codex_primary_reset_after_seconds', '')::numeric,
          'window_minutes', nullif(a.extra->>'codex_primary_window_minutes', '')::numeric
        ),
        'secondary', jsonb_build_object(
          'used_percent', nullif(a.extra->>'codex_secondary_used_percent', '')::numeric,
          'reset_after_seconds', nullif(a.extra->>'codex_secondary_reset_after_seconds', '')::numeric,
          'window_minutes', nullif(a.extra->>'codex_secondary_window_minutes', '')::numeric
        ),
        'primary_over_secondary_percent', nullif(a.extra->>'codex_primary_over_secondary_percent', '')::numeric
      ),
      'metrics', jsonb_build_object(
        'today', jsonb_build_object('requests', ar.today_requests, 'total_tokens', ar.today_tokens, 'total_cost', ar.today_total_cost, 'account_cost', ar.today_account_cost),
        'last_7d', jsonb_build_object('requests', ar.requests_7d, 'total_tokens', ar.tokens_7d, 'total_cost', ar.total_cost_7d, 'account_cost', ar.account_cost_7d),
        'last_30d', jsonb_build_object('requests', ar.requests_30d, 'total_tokens', ar.tokens_30d, 'total_cost', ar.total_cost_30d, 'account_cost', ar.account_cost_30d, 'avg_duration_ms', ar.avg_duration_ms_30d)
      ),
      'top_models_7d', coalesce((
        SELECT jsonb_agg(jsonb_build_object('model', model, 'requests', requests, 'total_cost', total_cost, 'total_tokens', total_tokens) ORDER BY requests DESC)
        FROM account_model_ranked model_rows
        WHERE model_rows.account_id = a.id AND model_rows.rank <= 5
      ), '[]'::jsonb)
    ) ORDER BY
      CASE WHEN a.status = 'active' THEN 0 ELSE 1 END,
      a.schedulable DESC,
      ar.requests_30d DESC,
      a.id)
    FROM accounts a
    JOIN account_rollups ar ON ar.id = a.id
    WHERE a.deleted_at IS NULL
  ), '[]'::jsonb),
  'users', coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'id', u.id,
      'username', nullif(u.username, ''),
      'email', u.email,
      'role', u.role,
      'status', u.status,
      'balance', round(u.balance::numeric, 6),
      'concurrency', u.concurrency,
      'rpm_limit', u.rpm_limit,
      'created_at', u.created_at,
      'updated_at', u.updated_at,
      'last_login_at', u.last_login_at,
      'last_active_at', u.last_active_at,
      'last_request_at', ur.last_request_at,
      'api_key_count', coalesce(kc.api_key_count, 0),
      'active_api_key_count', coalesce(kc.active_api_key_count, 0),
      'last_key_used_at', kc.last_key_used_at,
      'active_subscription_count', coalesce(sc.active_subscription_count, 0),
      'active_subscription_expires_at', sc.active_subscription_expires_at,
      'active_monthly_usage_usd', coalesce(sc.active_monthly_usage_usd, 0),
      'metrics', jsonb_build_object(
        'today', jsonb_build_object('requests', ur.today_requests, 'total_tokens', ur.today_tokens, 'total_cost', ur.today_total_cost, 'account_cost', ur.today_account_cost),
        'last_7d', jsonb_build_object('requests', ur.requests_7d, 'total_tokens', ur.tokens_7d, 'total_cost', ur.total_cost_7d, 'account_cost', ur.account_cost_7d),
        'last_30d', jsonb_build_object('requests', ur.requests_30d, 'total_tokens', ur.tokens_30d, 'total_cost', ur.total_cost_30d, 'account_cost', ur.account_cost_30d, 'avg_duration_ms', ur.avg_duration_ms_30d, 'accounts_used', ur.accounts_used_30d)
      ),
      'top_models_7d', coalesce((
        SELECT jsonb_agg(jsonb_build_object('model', model, 'requests', requests, 'total_cost', total_cost, 'total_tokens', total_tokens) ORDER BY requests DESC)
        FROM user_model_ranked model_rows
        WHERE model_rows.user_id = u.id AND model_rows.rank <= 4
      ), '[]'::jsonb)
    ) ORDER BY ur.requests_30d DESC, ur.total_cost_30d DESC, u.id)
    FROM users u
    JOIN user_rollups ur ON ur.id = u.id
    LEFT JOIN api_key_counts kc ON kc.user_id = u.id
    LEFT JOIN subscription_counts sc ON sc.user_id = u.id
    WHERE u.deleted_at IS NULL
  ), '[]'::jsonb),
  'daily', coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'date', to_char(bucket_start, 'YYYY-MM-DD'),
      'bucket_start', bucket_start,
      'requests', requests,
      'total_tokens', total_tokens,
      'total_cost', total_cost,
      'account_cost', account_cost,
      'active_users', active_users,
      'accounts_used', accounts_used
    ) ORDER BY bucket_start)
    FROM daily_rollups
  ), '[]'::jsonb),
  'hourly', coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'bucket_start', bucket_start,
      'label', to_char(bucket_start, 'MM-DD HH24:00'),
      'requests', requests,
      'total_tokens', total_tokens,
      'total_cost', total_cost,
      'active_users', active_users
    ) ORDER BY bucket_start)
    FROM hourly_rollups
  ), '[]'::jsonb),
  'top_models_30d', coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'model', model,
      'requests', requests,
      'total_tokens', total_tokens,
      'total_cost', total_cost,
      'active_users', active_users
    ) ORDER BY requests DESC)
    FROM (
      SELECT
        ul.model,
        count(*)::bigint AS requests,
        coalesce(sum(ul.input_tokens + ul.output_tokens + ul.cache_creation_tokens + ul.cache_read_tokens + ul.image_output_tokens), 0)::bigint AS total_tokens,
        coalesce(round(sum(ul.total_cost)::numeric, 6), 0) AS total_cost,
        count(DISTINCT ul.user_id)::bigint AS active_users
      FROM logs_30d ul
      GROUP BY ul.model
      ORDER BY requests DESC
      LIMIT 12
    ) model_rows
  ), '[]'::jsonb),
  'status_counts', jsonb_build_object(
    'accounts', coalesce((SELECT jsonb_object_agg(status, count) FROM (SELECT status, count(*)::bigint AS count FROM accounts WHERE deleted_at IS NULL GROUP BY status) s), '{}'::jsonb),
    'users', coalesce((SELECT jsonb_object_agg(status, count) FROM (SELECT status, count(*)::bigint AS count FROM users WHERE deleted_at IS NULL GROUP BY status) s), '{}'::jsonb)
  )
));

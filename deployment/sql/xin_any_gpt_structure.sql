-- ============================================================
-- any-auto-register PostgreSQL 初始化脚本
-- 适用于 SQLModel 定义，应用首次启动也可自动建表
-- 此脚本用于全新数据库的预初始化
-- ============================================================

-- 按依赖顺序建表
BEGIN;

-- users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR NOT NULL UNIQUE,
    password_hash VARCHAR NOT NULL,
    role VARCHAR NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_users_username ON users(username);
CREATE INDEX IF NOT EXISTS ix_users_role ON users(role);

-- configs
CREATE TABLE IF NOT EXISTS configs (
    key VARCHAR NOT NULL PRIMARY KEY,
    value VARCHAR NOT NULL DEFAULT ''
);

-- proxies
CREATE TABLE IF NOT EXISTS proxies (
    id SERIAL PRIMARY KEY,
    url VARCHAR NOT NULL UNIQUE,
    region VARCHAR NOT NULL DEFAULT '',
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_checked TIMESTAMP DEFAULT NULL
);

-- accounts
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    platform VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    password VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL DEFAULT '',
    region VARCHAR NOT NULL DEFAULT '',
    token VARCHAR NOT NULL DEFAULT '',
    status VARCHAR NOT NULL DEFAULT 'registered',
    trial_end_time INTEGER NOT NULL DEFAULT 0,
    cashier_url VARCHAR NOT NULL DEFAULT '',
    extra_json VARCHAR NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_accounts_platform ON accounts(platform);
CREATE INDEX IF NOT EXISTS ix_accounts_email ON accounts(email);

-- outlook_accounts
CREATE TABLE IF NOT EXISTS outlook_accounts (
    id SERIAL PRIMARY KEY,
    email VARCHAR NOT NULL UNIQUE,
    password VARCHAR NOT NULL,
    client_id VARCHAR NOT NULL DEFAULT '',
    refresh_token VARCHAR NOT NULL DEFAULT '',
    account_type VARCHAR NOT NULL DEFAULT 'microsoft_oauth',
    mailapi_url VARCHAR NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_used TIMESTAMP DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS ix_outlook_accounts_email ON outlook_accounts(email);

-- temp_mailboxes
CREATE TABLE IF NOT EXISTS temp_mailboxes (
    id SERIAL PRIMARY KEY,
    email VARCHAR NOT NULL,
    provider VARCHAR NOT NULL DEFAULT '',
    account_id VARCHAR NOT NULL DEFAULT '',
    extra_json VARCHAR NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_temp_mailboxes_email ON temp_mailboxes(email);

-- task_logs
CREATE TABLE IF NOT EXISTS task_logs (
    id SERIAL PRIMARY KEY,
    platform VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    error VARCHAR NOT NULL DEFAULT '',
    detail_json VARCHAR NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- task_runs
CREATE TABLE IF NOT EXISTS task_runs (
    id VARCHAR NOT NULL PRIMARY KEY,
    platform VARCHAR NOT NULL,
    source VARCHAR NOT NULL DEFAULT 'manual',
    status VARCHAR NOT NULL DEFAULT 'pending',
    total INTEGER NOT NULL DEFAULT 0,
    progress VARCHAR NOT NULL DEFAULT '0/0',
    success INTEGER NOT NULL DEFAULT 0,
    registered INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    error VARCHAR NOT NULL DEFAULT '',
    meta_json VARCHAR NOT NULL DEFAULT '{}',
    logs_json VARCHAR NOT NULL DEFAULT '[]',
    errors_json VARCHAR NOT NULL DEFAULT '[]',
    cashier_urls_json VARCHAR NOT NULL DEFAULT '[]',
    control_json VARCHAR NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_task_runs_platform ON task_runs(platform);
CREATE INDEX IF NOT EXISTS ix_task_runs_source ON task_runs(source);
CREATE INDEX IF NOT EXISTS ix_task_runs_status ON task_runs(status);
CREATE INDEX IF NOT EXISTS ix_task_runs_created_at ON task_runs(created_at);
CREATE INDEX IF NOT EXISTS ix_task_runs_updated_at ON task_runs(updated_at);

-- sk_api_keys (depends on users, proxies)
CREATE TABLE IF NOT EXISTS sk_api_keys (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name VARCHAR NOT NULL,
    description VARCHAR NOT NULL DEFAULT '',
    key_prefix VARCHAR NOT NULL,
    key_hash VARCHAR NOT NULL UNIQUE,
    target_url VARCHAR NOT NULL DEFAULT '',
    upstream_api_key VARCHAR NOT NULL DEFAULT '',
    proxy_id INTEGER DEFAULT NULL REFERENCES proxies(id),
    proxy_url VARCHAR NOT NULL DEFAULT '',
    token_limit INTEGER NOT NULL DEFAULT 0,
    prompt_tokens_used INTEGER NOT NULL DEFAULT 0,
    completion_tokens_used INTEGER NOT NULL DEFAULT 0,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMP DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sk_api_keys_user_id ON sk_api_keys(user_id);
CREATE INDEX IF NOT EXISTS ix_sk_api_keys_name ON sk_api_keys(name);
CREATE INDEX IF NOT EXISTS ix_sk_api_keys_key_prefix ON sk_api_keys(key_prefix);
CREATE INDEX IF NOT EXISTS ix_sk_api_keys_key_hash ON sk_api_keys(key_hash);
CREATE INDEX IF NOT EXISTS ix_sk_api_keys_proxy_id ON sk_api_keys(proxy_id);

-- sk_api_key_usage_logs (depends on sk_api_keys, users)
CREATE TABLE IF NOT EXISTS sk_api_key_usage_logs (
    id SERIAL PRIMARY KEY,
    api_key_id INTEGER NOT NULL REFERENCES sk_api_keys(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    model VARCHAR NOT NULL DEFAULT '',
    target_url VARCHAR NOT NULL DEFAULT '',
    proxy_url VARCHAR NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error VARCHAR NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sk_api_key_usage_logs_api_key_id ON sk_api_key_usage_logs(api_key_id);
CREATE INDEX IF NOT EXISTS ix_sk_api_key_usage_logs_user_id ON sk_api_key_usage_logs(user_id);

-- api_access_logs (depends on users, sk_api_keys)
CREATE TABLE IF NOT EXISTS api_access_logs (
    id SERIAL PRIMARY KEY,
    actor_type VARCHAR NOT NULL DEFAULT 'anonymous',
    user_id INTEGER DEFAULT NULL REFERENCES users(id),
    username VARCHAR NOT NULL DEFAULT '',
    api_key_id INTEGER DEFAULT NULL REFERENCES sk_api_keys(id),
    api_key_name VARCHAR NOT NULL DEFAULT '',
    api_key_prefix VARCHAR NOT NULL DEFAULT '',
    method VARCHAR NOT NULL DEFAULT '',
    path VARCHAR NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL DEFAULT 200,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    client_ip VARCHAR NOT NULL DEFAULT '',
    user_agent VARCHAR NOT NULL DEFAULT '',
    target_url VARCHAR NOT NULL DEFAULT '',
    model VARCHAR NOT NULL DEFAULT '',
    error VARCHAR NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_actor_type ON api_access_logs(actor_type);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_user_id ON api_access_logs(user_id);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_username ON api_access_logs(username);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_api_key_id ON api_access_logs(api_key_id);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_api_key_prefix ON api_access_logs(api_key_prefix);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_method ON api_access_logs(method);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_path ON api_access_logs(path);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_status_code ON api_access_logs(status_code);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_success ON api_access_logs(success);
CREATE INDEX IF NOT EXISTS ix_api_access_logs_created_at ON api_access_logs(created_at);

COMMIT;

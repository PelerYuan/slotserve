"""Catch-up for every database stamped ``user_version`` 1: SlotServe 0.3 to 0.8 and builds of main after 0.8.0.

Until schema versions were numbered, ``create_all`` created new tables and added new nullable columns on every
start, so a version-1 database can have any schema from 0.3 to the one numbering began with. This step makes them
all the same: the frozen schema below (exactly what ``Base.metadata.create_all`` produced at that point).
Nothing is rebuilt and no rows are touched; columns added to existing tables have no foreign key clause (SQLite
cannot add one), as before.
"""

import sqlite3

from . import MigrationReport
from ._util import apply_schema

VERSION = 3

SCHEMA = """
CREATE TABLE users (
    id INTEGER NOT NULL,
    username VARCHAR(32) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_admin BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    disabled_at DATETIME,
    locale VARCHAR(8),
    telegram_chat_id BIGINT,
    telegram_username VARCHAR(64),
    telegram_link_token VARCHAR(64),
    notify_prefs TEXT,
    channels_off TEXT,
    ical_token VARCHAR(64),
    devenv_password_hash VARCHAR(255),
    PRIMARY KEY (id)
);
CREATE INDEX ix_users_ical_token ON users (ical_token);
CREATE INDEX ix_users_telegram_link_token ON users (telegram_link_token);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE TABLE app_settings (
    "key" VARCHAR(64) NOT NULL,
    value TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY ("key")
);
CREATE TABLE sent_notifications (
    "key" VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY ("key")
);
CREATE INDEX ix_sent_notifications_created_at ON sent_notifications (created_at);
CREATE TABLE kv_store (
    "key" VARCHAR(64) NOT NULL,
    value TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY ("key")
);
CREATE TABLE notification_outbox (
    id INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_notification_outbox_next_attempt_at ON notification_outbox (next_attempt_at);
CREATE TABLE storages (
    id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    kind VARCHAR(8) NOT NULL,
    purpose VARCHAR(16) NOT NULL,
    host_path TEXT,
    server VARCHAR(255),
    remote_path TEXT,
    options TEXT,
    username VARCHAR(128),
    password_enc TEXT,
    mount_path VARCHAR(255),
    read_only BOOLEAN NOT NULL,
    access VARCHAR(16) NOT NULL,
    user_ids TEXT,
    local_path TEXT,
    quota_gb FLOAT,
    enabled BOOLEAN NOT NULL,
    status TEXT,
    usage TEXT,
    warned TEXT,
    checked_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name)
);
CREATE TABLE api_keys (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    prefix VARCHAR(16) NOT NULL,
    key_hash VARCHAR(64) NOT NULL,
    key_encrypted TEXT,
    created_at DATETIME NOT NULL,
    last_used_at DATETIME,
    revoked_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_api_keys_user_id ON api_keys (user_id);
CREATE UNIQUE INDEX ix_api_keys_key_hash ON api_keys (key_hash);
CREATE TABLE models (
    id INTEGER NOT NULL,
    name VARCHAR(128) NOT NULL,
    file_path TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_default BOOLEAN NOT NULL,
    args TEXT NOT NULL,
    description TEXT NOT NULL,
    engine VARCHAR(16),
    created_by INTEGER,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX ix_models_name ON models (name);
CREATE TABLE catalog_entries (
    id INTEGER NOT NULL,
    entry_id VARCHAR(64) NOT NULL,
    data TEXT NOT NULL,
    created_by INTEGER,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (entry_id),
    FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE audit_logs (
    id INTEGER NOT NULL,
    actor_id INTEGER,
    actor_name VARCHAR(32) NOT NULL,
    action VARCHAR(64) NOT NULL,
    target VARCHAR(160) NOT NULL,
    detail TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);
CREATE INDEX ix_audit_logs_created_at ON audit_logs (created_at);
CREATE TABLE usage_daily (
    date VARCHAR(10) NOT NULL,
    user_id INTEGER NOT NULL,
    model_name VARCHAR(128) NOT NULL,
    requests INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    prompt_tokens BIGINT NOT NULL,
    completion_tokens BIGINT NOT NULL,
    duration_ms BIGINT NOT NULL,
    PRIMARY KEY (date, user_id, model_name),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE chat_links (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    channel VARCHAR(16) NOT NULL,
    address VARCHAR(128),
    display_name VARCHAR(128),
    code VARCHAR(16),
    code_expires_at DATETIME,
    linked_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (user_id, channel),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_chat_links_channel ON chat_links (channel);
CREATE INDEX ix_chat_links_code ON chat_links (code);
CREATE INDEX ix_chat_links_address ON chat_links (address);
CREATE INDEX ix_chat_links_user_id ON chat_links (user_id);
CREATE TABLE ssh_keys (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    public_key TEXT NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_ssh_keys_user_id ON ssh_keys (user_id);
CREATE TABLE devenv_templates (
    id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    image VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    command TEXT,
    env TEXT,
    web_ports TEXT,
    keep_entrypoint BOOLEAN,
    kind VARCHAR(16) NOT NULL,
    builtin_key VARCHAR(32),
    owner_id INTEGER,
    enabled BOOLEAN NOT NULL,
    persist_paths TEXT,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (builtin_key),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_devenv_templates_owner_id ON devenv_templates (owner_id);
CREATE TABLE model_presets (
    id INTEGER NOT NULL,
    model_id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    args TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (model_id, name),
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE
);
CREATE INDEX ix_model_presets_model_id ON model_presets (model_id);
CREATE TABLE download_tasks (
    id INTEGER NOT NULL,
    user_id INTEGER,
    url TEXT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    downloaded_bytes BIGINT NOT NULL,
    total_bytes BIGINT,
    speed_bps DOUBLE NOT NULL,
    error TEXT,
    model_id INTEGER,
    created_at DATETIME NOT NULL,
    finished_at DATETIME,
    storage_id INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE SET NULL,
    FOREIGN KEY(storage_id) REFERENCES storages (id) ON DELETE SET NULL
);
CREATE INDEX ix_download_tasks_status ON download_tasks (status);
CREATE TABLE lanes (
    id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    gpu_indices VARCHAR(64) NOT NULL,
    default_model_id INTEGER,
    position INTEGER NOT NULL,
    idle_mode VARCHAR(16),
    idle_template_id INTEGER,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name),
    FOREIGN KEY(default_model_id) REFERENCES models (id) ON DELETE SET NULL,
    FOREIGN KEY(idle_template_id) REFERENCES devenv_templates (id) ON DELETE SET NULL
);
CREATE TABLE devenv_projects (
    id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    template_id INTEGER,
    image VARCHAR(255) NOT NULL,
    persist_paths TEXT,
    state VARCHAR(16) NOT NULL,
    container_id VARCHAR(80),
    container_devices VARCHAR(64),
    container_paths TEXT,
    container_port INTEGER,
    container_web TEXT,
    container_storage VARCHAR(64),
    size_bytes BIGINT,
    last_used_at DATETIME,
    created_at DATETIME NOT NULL,
    cpu_after_booking BOOLEAN,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(template_id) REFERENCES devenv_templates (id) ON DELETE SET NULL
);
CREATE INDEX ix_devenv_projects_owner_id ON devenv_projects (owner_id);
CREATE TABLE catalog_deploys (
    id INTEGER NOT NULL,
    user_id INTEGER,
    entry_id VARCHAR(64) NOT NULL,
    variant_id VARCHAR(64) NOT NULL,
    engine VARCHAR(16) NOT NULL,
    model_name VARCHAR(128) NOT NULL,
    args TEXT NOT NULL,
    description TEXT NOT NULL,
    target_path TEXT NOT NULL,
    lane_id INTEGER,
    set_default BOOLEAN NOT NULL,
    task_ids TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    error TEXT,
    model_id INTEGER,
    created_at DATETIME NOT NULL,
    finished_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL,
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE SET NULL
);
CREATE INDEX ix_catalog_deploys_status ON catalog_deploys (status);
CREATE TABLE reservations (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    model_id INTEGER,
    preset_id INTEGER,
    series_id VARCHAR(36),
    lane_id INTEGER,
    devenv_template_id INTEGER,
    devenv_project_id INTEGER,
    start_at DATETIME NOT NULL,
    end_at DATETIME NOT NULL,
    status VARCHAR(16) NOT NULL,
    reviewed_by VARCHAR(32),
    review_note TEXT,
    note TEXT NOT NULL,
    released_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE RESTRICT,
    FOREIGN KEY(preset_id) REFERENCES model_presets (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL,
    FOREIGN KEY(devenv_template_id) REFERENCES devenv_templates (id) ON DELETE SET NULL,
    FOREIGN KEY(devenv_project_id) REFERENCES devenv_projects (id) ON DELETE SET NULL
);
CREATE INDEX ix_reservations_start_at ON reservations (start_at);
CREATE INDEX ix_reservations_series_id ON reservations (series_id);
CREATE INDEX ix_reservations_end_at ON reservations (end_at);
CREATE INDEX ix_reservations_status ON reservations (status);
CREATE INDEX ix_reservations_model_id ON reservations (model_id);
CREATE INDEX ix_reservations_user_id ON reservations (user_id);
CREATE INDEX ix_reservations_lane_id ON reservations (lane_id);
CREATE INDEX ix_reservations_devenv_project_id ON reservations (devenv_project_id);
CREATE TABLE model_load_events (
    id INTEGER NOT NULL,
    model_id INTEGER,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NOT NULL,
    success BOOLEAN NOT NULL,
    error TEXT,
    lane_id INTEGER,
    command TEXT,
    exit_code INTEGER,
    log_excerpt TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
);
CREATE INDEX ix_model_load_events_model_id ON model_load_events (model_id);
CREATE TABLE usage_logs (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    api_key_id INTEGER,
    reservation_id INTEGER,
    model_name VARCHAR(128) NOT NULL,
    lane_id INTEGER,
    endpoint VARCHAR(128) NOT NULL,
    stream BOOLEAN NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(api_key_id) REFERENCES api_keys (id) ON DELETE SET NULL,
    FOREIGN KEY(reservation_id) REFERENCES reservations (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
);
CREATE INDEX ix_usage_logs_user_id ON usage_logs (user_id);
CREATE INDEX ix_usage_logs_created_at ON usage_logs (created_at);
CREATE TABLE waitlist (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    model_id INTEGER,
    preset_id INTEGER,
    lane_id INTEGER,
    start_at DATETIME NOT NULL,
    end_at DATETIME NOT NULL,
    note TEXT NOT NULL,
    auto_book BOOLEAN NOT NULL,
    status VARCHAR(16) NOT NULL,
    reservation_id INTEGER,
    created_at DATETIME NOT NULL,
    resolved_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE,
    FOREIGN KEY(preset_id) REFERENCES model_presets (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL,
    FOREIGN KEY(reservation_id) REFERENCES reservations (id) ON DELETE SET NULL
);
CREATE INDEX ix_waitlist_status ON waitlist (status);
CREATE INDEX ix_waitlist_user_id ON waitlist (user_id);
CREATE TABLE devenv_sessions (
    id INTEGER NOT NULL,
    kind VARCHAR(16),
    reservation_id INTEGER,
    user_id INTEGER,
    project_id INTEGER,
    template_id INTEGER,
    lane_id INTEGER,
    image VARCHAR(255) NOT NULL,
    state VARCHAR(16) NOT NULL,
    detail TEXT,
    progress DOUBLE,
    error TEXT,
    container_id VARCHAR(80),
    container_name VARCHAR(80),
    ssh_port INTEGER,
    web TEXT,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    stopped_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (reservation_id),
    FOREIGN KEY(reservation_id) REFERENCES reservations (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(project_id) REFERENCES devenv_projects (id) ON DELETE SET NULL,
    FOREIGN KEY(template_id) REFERENCES devenv_templates (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
);
CREATE INDEX ix_devenv_sessions_user_id ON devenv_sessions (user_id);
CREATE TABLE usage_samples (
    id INTEGER NOT NULL,
    start DATETIME NOT NULL,
    hourly BOOLEAN NOT NULL,
    seconds INTEGER NOT NULL,
    resource VARCHAR(8) NOT NULL,
    lane_id INTEGER,
    gpu_index INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,
    user_id INTEGER,
    reservation_id INTEGER,
    model_name VARCHAR(128),
    metered_seconds INTEGER NOT NULL,
    util_seconds FLOAT NOT NULL,
    busy_seconds INTEGER NOT NULL,
    energy_wh FLOAT NOT NULL,
    mem_peak_mb INTEGER,
    mem_total_mb INTEGER,
    cpu_core_seconds FLOAT,
    PRIMARY KEY (id),
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(reservation_id) REFERENCES reservations (id) ON DELETE SET NULL
);
CREATE INDEX ix_usage_samples_start_lane ON usage_samples (start, lane_id);
CREATE INDEX ix_usage_samples_user_id ON usage_samples (user_id);
CREATE TABLE devenv_ports (
    id INTEGER NOT NULL,
    owner_id INTEGER,
    project_id INTEGER,
    session_id INTEGER,
    container_port INTEGER NOT NULL,
    name VARCHAR(32) NOT NULL,
    mode VARCHAR(8) NOT NULL,
    declared BOOLEAN NOT NULL,
    host_port INTEGER,
    error TEXT,
    active_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(project_id) REFERENCES devenv_projects (id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES devenv_sessions (id) ON DELETE CASCADE,
    UNIQUE (host_port)
);
CREATE INDEX ix_devenv_ports_owner_id ON devenv_ports (owner_id);
CREATE INDEX ix_devenv_ports_session_id ON devenv_ports (session_id);
CREATE INDEX ix_devenv_ports_project_id ON devenv_ports (project_id);
"""


def up(con: sqlite3.Connection) -> MigrationReport | None:
    report = apply_schema(con, SCHEMA)
    return report if report.affected else None

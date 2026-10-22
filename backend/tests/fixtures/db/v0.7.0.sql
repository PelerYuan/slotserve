PRAGMA user_version=1;
BEGIN TRANSACTION;
CREATE TABLE api_keys (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	prefix VARCHAR(16) NOT NULL, 
	key_hash VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_used_at DATETIME, 
	revoked_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
INSERT INTO "api_keys" VALUES(1,1,'name-0','sk-fx0','fd6292861bf6d05a74407c99a740cbe3b78882614210bccfc32e55791ac3aa1e','2026-01-05 08:00:00.000000',NULL,NULL);
INSERT INTO "api_keys" VALUES(2,2,'name-1','sk-fx1','1f1970bddbbed943d34fa13cee17b69d3a74d2e43749b84fc67ad9db9ce1795a','2026-01-05 08:00:00.000000',NULL,NULL);
INSERT INTO "api_keys" VALUES(3,3,'name-2','sk-fx2','9a88bddc3086f55377d3eac77d1a8a2aeab59ec1426d064a013aca41a66ae057','2026-01-05 08:00:00.000000',NULL,'2026-01-05 08:00:00.000000');
CREATE TABLE app_settings (
	"key" VARCHAR(64) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);
INSERT INTO "app_settings" VALUES('registration_open','false','2026-01-05 08:00:00');
INSERT INTO "app_settings" VALUES('max_reservation_hours','8','2026-01-05 08:00:00');
INSERT INTO "app_settings" VALUES('timezone','"UTC"','2026-01-05 08:00:00');
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
INSERT INTO "audit_logs" VALUES(1,1,'actor_name-0','user.register','target-0','{}','2026-01-05 08:00:00.000000');
INSERT INTO "audit_logs" VALUES(2,2,'actor_name-1','model.update','target-1','{}','2026-01-05 08:00:00.000000');
INSERT INTO "audit_logs" VALUES(3,3,'actor_name-2','settings.update','target-2','{}','2026-01-05 08:00:00.000000');
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
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE SET NULL
);
INSERT INTO "download_tasks" VALUES(1,1,'https://example.invalid/fixture-0.gguf','filename-0','done',10,NULL,1.5,NULL,1,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "download_tasks" VALUES(2,2,'https://example.invalid/fixture-1.gguf','filename-1','failed',20,NULL,1.5,NULL,2,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "download_tasks" VALUES(3,3,'https://example.invalid/fixture-2.gguf','filename-2','cancelled',30,NULL,1.5,NULL,3,'2026-01-05 08:00:00.000000',NULL);
CREATE TABLE kv_store (
	"key" VARCHAR(64) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);
INSERT INTO "kv_store" VALUES('fixture-0','{}','2026-01-05 08:00:00.000000');
INSERT INTO "kv_store" VALUES('fixture-1','{}','2026-01-05 08:00:00.000000');
INSERT INTO "kv_store" VALUES('fixture-2','{}','2026-01-05 08:00:00.000000');
CREATE TABLE lanes (
	id INTEGER NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	gpu_indices VARCHAR(64) NOT NULL, 
	default_model_id INTEGER, 
	position INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	FOREIGN KEY(default_model_id) REFERENCES models (id) ON DELETE SET NULL
);
INSERT INTO "lanes" VALUES(1,'GPU 0','0',NULL,0,'2026-10-22 03:18:31.409623');
INSERT INTO "lanes" VALUES(2,'Fixture GPU 2','2',2,20,'2026-01-05 08:00:00.000000');
CREATE TABLE model_load_events (
	id INTEGER NOT NULL, 
	model_id INTEGER, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME NOT NULL, 
	success BOOLEAN NOT NULL, 
	error TEXT, 
	lane_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE, 
	FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
);
INSERT INTO "model_load_events" VALUES(1,1,'2026-01-05 08:00:00.000000','2026-01-05 09:00:00.000000',1,NULL,1);
INSERT INTO "model_load_events" VALUES(2,2,'2026-01-05 08:00:00.000000','2026-01-05 09:00:00.000000',0,NULL,2);
INSERT INTO "model_load_events" VALUES(3,3,'2026-01-05 08:00:00.000000','2026-01-05 09:00:00.000000',1,NULL,1);
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
INSERT INTO "model_presets" VALUES(1,1,'name-0','-c 8192','2026-01-05 08:00:00.000000');
INSERT INTO "model_presets" VALUES(2,2,'name-1','-c 8192','2026-01-05 08:00:00.000000');
INSERT INTO "model_presets" VALUES(3,3,'name-2','-c 8192','2026-01-05 08:00:00.000000');
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
INSERT INTO "models" VALUES(1,'fixture-model-0-Q4_K_M','fixture-model-0-Q4_K_M.gguf',10,'ready',1,'-c 4096','description-0',NULL,1,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "models" VALUES(2,'fixture-model-1-Q4_K_M','fixture-model-1-Q4_K_M.gguf',20,'ready',0,'-c 4096','description-1',NULL,2,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "models" VALUES(3,'fixture-model-2-Q4_K_M','fixture-model-2-Q4_K_M.gguf',30,'ready',0,'-c 4096','description-2',NULL,3,'2026-01-05 08:00:00.000000',NULL);
CREATE TABLE notification_outbox (
	id INTEGER NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	payload TEXT NOT NULL, 
	attempts INTEGER NOT NULL, 
	next_attempt_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE reservations (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	model_id INTEGER, 
	preset_id INTEGER, 
	series_id VARCHAR(36), 
	lane_id INTEGER, 
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
	FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
);
INSERT INTO "reservations" VALUES(1,1,1,1,NULL,1,'2026-01-06 08:00:00.000000','2026-01-06 10:00:00.000000','booked',NULL,NULL,'note-0',NULL,'2026-01-05 08:00:00.000000');
INSERT INTO "reservations" VALUES(2,2,2,2,NULL,2,'2026-01-07 08:00:00.000000','2026-01-07 10:00:00.000000','cancelled',NULL,NULL,'note-1',NULL,'2026-01-05 08:00:00.000000');
INSERT INTO "reservations" VALUES(3,3,3,3,NULL,1,'2026-01-08 08:00:00.000000','2026-01-08 10:00:00.000000','booked',NULL,NULL,'note-2',NULL,'2026-01-05 08:00:00.000000');
CREATE TABLE sent_notifications (
	"key" VARCHAR(128) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);
INSERT INTO "sent_notifications" VALUES('reminder:1','2026-01-05 08:00:00.000000');
INSERT INTO "sent_notifications" VALUES('reminder:2','2026-01-05 08:00:00.000000');
INSERT INTO "sent_notifications" VALUES('reminder:3','2026-01-05 08:00:00.000000');
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
INSERT INTO "usage_daily" VALUES('2025-12-01',1,'fixture-model-0-Q4_K_M',10,10,10,10,10);
INSERT INTO "usage_daily" VALUES('2025-12-02',2,'fixture-model-1-Q4_K_M',20,20,20,20,20);
INSERT INTO "usage_daily" VALUES('2025-12-03',3,'fixture-model-2-Q4_K_M',30,30,30,30,30);
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
INSERT INTO "usage_logs" VALUES(1,1,1,1,'model_name-0',1,'/v1/chat/completions',0,10,10,10,200,'2026-01-05 08:00:00.000000');
INSERT INTO "usage_logs" VALUES(2,2,2,2,'model_name-1',2,'/v1/chat/completions',1,20,20,20,200,'2026-01-05 08:00:00.000000');
INSERT INTO "usage_logs" VALUES(3,3,3,3,'model_name-2',1,'/v1/chat/completions',0,30,30,30,200,'2026-01-05 08:00:00.000000');
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
	ical_token VARCHAR(64), 
	PRIMARY KEY (id)
);
INSERT INTO "users" VALUES(1,'admin','scrypt$1f3f2523009cbfd6115dedc031f24f98$7c89ef5b3691960989b69f66e5776ce006dac1ba41cd086594ec4978a782e2f585fe24df0accc31dda5f851fb4f51e463f7ece3cb22e60b56e375c4301435fbd',1,'2026-01-05 08:00:00.000000',NULL,'en',10,'telegram_username-0','telegram_link_token-0','{"reminders": true}','ical000000000000000000000000000000000000000000000000000000000000');
INSERT INTO "users" VALUES(2,'alice','scrypt$1f3f2523009cbfd6115dedc031f24f98$7c89ef5b3691960989b69f66e5776ce006dac1ba41cd086594ec4978a782e2f585fe24df0accc31dda5f851fb4f51e463f7ece3cb22e60b56e375c4301435fbd',0,'2026-01-05 08:00:00.000000',NULL,'zh-CN',20,'telegram_username-1','telegram_link_token-1','{"reminders": true}','ical000000000000000000000000000000000000000000000000000000000001');
INSERT INTO "users" VALUES(3,'bob','scrypt$1f3f2523009cbfd6115dedc031f24f98$7c89ef5b3691960989b69f66e5776ce006dac1ba41cd086594ec4978a782e2f585fe24df0accc31dda5f851fb4f51e463f7ece3cb22e60b56e375c4301435fbd',0,'2026-01-05 08:00:00.000000',NULL,NULL,30,'telegram_username-2','telegram_link_token-2','{"reminders": true}','ical000000000000000000000000000000000000000000000000000000000002');
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
INSERT INTO "waitlist" VALUES(1,1,1,1,1,'2026-01-06 08:00:00.000000','2026-01-06 10:00:00.000000','note-0',0,'waiting',1,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "waitlist" VALUES(2,2,2,2,2,'2026-01-07 08:00:00.000000','2026-01-07 10:00:00.000000','note-1',1,'expired',2,'2026-01-05 08:00:00.000000',NULL);
INSERT INTO "waitlist" VALUES(3,3,3,3,1,'2026-01-08 08:00:00.000000','2026-01-08 10:00:00.000000','note-2',0,'cancelled',3,'2026-01-05 08:00:00.000000',NULL);
CREATE INDEX ix_users_ical_token ON users (ical_token);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE INDEX ix_users_telegram_link_token ON users (telegram_link_token);
CREATE INDEX ix_sent_notifications_created_at ON sent_notifications (created_at);
CREATE INDEX ix_notification_outbox_next_attempt_at ON notification_outbox (next_attempt_at);
CREATE UNIQUE INDEX ix_api_keys_key_hash ON api_keys (key_hash);
CREATE INDEX ix_api_keys_user_id ON api_keys (user_id);
CREATE UNIQUE INDEX ix_models_name ON models (name);
CREATE INDEX ix_audit_logs_created_at ON audit_logs (created_at);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);
CREATE INDEX ix_model_presets_model_id ON model_presets (model_id);
CREATE INDEX ix_download_tasks_status ON download_tasks (status);
CREATE INDEX ix_reservations_series_id ON reservations (series_id);
CREATE INDEX ix_reservations_user_id ON reservations (user_id);
CREATE INDEX ix_reservations_status ON reservations (status);
CREATE INDEX ix_reservations_lane_id ON reservations (lane_id);
CREATE INDEX ix_reservations_start_at ON reservations (start_at);
CREATE INDEX ix_reservations_model_id ON reservations (model_id);
CREATE INDEX ix_reservations_end_at ON reservations (end_at);
CREATE INDEX ix_model_load_events_model_id ON model_load_events (model_id);
CREATE INDEX ix_usage_logs_created_at ON usage_logs (created_at);
CREATE INDEX ix_usage_logs_user_id ON usage_logs (user_id);
CREATE INDEX ix_waitlist_status ON waitlist (status);
CREATE INDEX ix_waitlist_user_id ON waitlist (user_id);
COMMIT;

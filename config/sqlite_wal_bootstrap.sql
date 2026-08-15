-- FlowGrid SQLite WAL 高写入初始化脚本。
-- 必须在数据库所在本机执行；WAL 不支持跨主机/网络文件系统共享。

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -65536;            -- KiB：约 64 MiB page cache
PRAGMA mmap_size = 268435456;          -- 256 MiB；按主机内存与安全策略调整
PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = ON;
PRAGMA wal_autocheckpoint = 0;         -- 交给独立 PASSIVE checkpoint worker
PRAGMA journal_size_limit = 67108864;  -- 64 MiB

-- 时间账本：当前事实读取与审计链扫描。
CREATE INDEX IF NOT EXISTS idx_claim_active_scope
ON temporal_claims(user_id, scope, revision DESC)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_claim_scope_revision
ON temporal_claims(user_id, scope, revision DESC);

-- 仅当查询常按用户筛选且表量较大时有帮助；用 EXPLAIN QUERY PLAN 验证后保留。
CREATE INDEX IF NOT EXISTS idx_messages_user_seq
ON messages(user_id, session_id, seq);

-- checkpoint worker 运行的语句（不要在每个 HTTP 请求里执行）。
-- PRAGMA wal_checkpoint(PASSIVE);

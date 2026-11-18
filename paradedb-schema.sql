-- ParadeDB personal-RAG schema  (database: myragdatabase)
-- Apply with:  docker exec -i paradedb psql -U myuser -d myragdatabase < paradedb-schema.sql

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: dense/semantic
CREATE EXTENSION IF NOT EXISTS pg_search;   -- ParadeDB BM25 (Tantivy): sparse/lexical

CREATE TABLE IF NOT EXISTS chunks (
  id           bigserial PRIMARY KEY,
  source_path  text NOT NULL,          -- file the chunk came from
  title        text,
  doc_type     text,                   -- 'obsidian' | 'pdf'
  chunk_index  int,
  chunk_text   text NOT NULL,          -- BM25 searches this
  metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {file, status, type, lab}
  embedding    vector(1024)            -- bge-m3; HNSW cosine index searches this
);

-- Keyword index (BM25)
CREATE INDEX IF NOT EXISTS chunks_bm25 ON chunks
USING bm25 (id, chunk_text) WITH (key_field='id');

-- Semantic index (HNSW, cosine)
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks
USING hnsw (embedding vector_cosine_ops);

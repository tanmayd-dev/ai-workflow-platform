# AI-Driven Enterprise Decision Workflow Platform

## Overview
A regulated AI-governed workflow orchestration system for banking operations. This system demonstrates how AI can assist in decision-making while maintaining strict governance, auditability, and regulatory compliance.

## Key Features
- **AI-Assisted Workflow Design**: LLMs draft workflows based on natural language intent
- **Hybrid Decision Control**: AI suggests, deterministic rules decide
- **Full Audit Trail**: Every decision is logged and replayable
- **Knowledge Base (RAG)**: Domain-agnostic via uploaded regulatory documents
- **Immutable Workflows**: Approved workflows are frozen and versioned
- **Account-Level Serialization**: One active run per account

## Tech Stack
- **Backend**: FastAPI (Python 3.10+)
- **Database**: SQLite + SQLAlchemy (async)
- **AI/LLM**: OpenRouter (pony-alpha model)
- **Vector Store**: ChromaDB + HuggingFace Embeddings
- **Orchestration**: LangGraph
- **Auth**: JWT + RBAC

## Installation

### Prerequisites
- Python 3.10+
- Virtual environment

### Setup
```bash
# Clone repository
git clone <repository-url>
cd "AI-Driven Enterprise Decision Workflow Platform"

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your OPENROUTER_API_KEY

# Run migrations
alembic upgrade head

# Start server
python -m app.main
```

## Usage

Server will be available at `http://localhost:8000`

- **API Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## Architecture

### Core Subsystems
1. **Knowledge Base (RAG)**: Upload domain-specific documents
2. **Workflow Definition**: Design and approve workflows
3. **Rules Engine**: Deterministic JSON DSL evaluation
4. **Execution Engine**: LangGraph-based state machine
5. **Audit & Replay**: Full execution tracking and verification

### Database Schema
- **Users & Roles**: RBAC authentication
- **Workflow Definitions**: Immutable, versioned workflows
- **Rules**: Global, versioned JSON DSL rules
- **Execution**: Runs, node executions, audit logs
- **Documents**: Knowledge base metadata

## Project Structure
```
app/
├── main.py                 # FastAPI entrypoint
├── core/                   # Config, database, security
├── models/                 # SQLAlchemy models
├── schemas/                # Pydantic schemas
├── services/               # Business logic
│   ├── rag_service.py     # Vector store & retrieval
│   ├── llm_service.py     # OpenRouter client
│   ├── rule_engine.py     # JSON DSL evaluator
│   └── compiler.py        # Workflow → LangGraph
└── api/v1/endpoints/       # REST API endpoints
```

## License
MIT

## Author
Tanmay Deshpande

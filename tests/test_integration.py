"""
Integration tests for the AI-Governed Workflow System.
Tests the full workflow lifecycle.
"""
import asyncio
import pytest
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session_maker
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType, WorkflowStatus
from app.models.user import User, Role
from app.services.executor import WorkflowExecutor
from app.core.security import get_password_hash


@pytest.fixture
async def db_session():
    """Provide a database session for tests."""
    async_session_maker = get_session_maker()
    async with async_session_maker() as session:
        yield session


@pytest.fixture
async def sample_user(db_session: AsyncSession):
    """Create a sample user."""
    user = User(
        id=uuid4(),
        email="test@example.com",
        hashed_password=get_password_hash("testpass123"),
        full_name="Test User",
        is_active=True
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def sample_workflow(db_session: AsyncSession, sample_user: User):
    """Create a sample workflow definition."""
    workflow = WorkflowDefinition(
        id=uuid4(),
        name="Test Fraud Workflow",
        description="Test workflow for integration testing",
        domain="fraud",
        version=1,
        status=WorkflowStatus.APPROVED,
        is_active=True,
        created_by=sample_user.id,
        approved_by=sample_user.id
    )
    db_session.add(workflow)
    await db_session.flush()
    
    # Add steps
    steps = [
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=0,
            step_type=StepType.AI_ANALYSIS,
            name="Risk Analysis",
            description="AI analyzes transaction risk",
            config_json={
                "rag_query": "fraud detection",
                "prompt_template": "Analyze this transaction for fraud risk"
            }
        ),
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=1,
            step_type=StepType.DOMAIN_RULE,
            name="Threshold Check",
            description="Apply risk threshold rules",
            config_json={
                "rule_id": "high_risk_threshold"
            }
        )
    ]
    
    for step in steps:
        db_session.add(step)
    
    await db_session.commit()
    await db_session.refresh(workflow)
    return workflow


@pytest.mark.asyncio
async def test_workflow_execution(db_session: AsyncSession, sample_workflow: WorkflowDefinition):
    """Test full workflow execution."""
    executor = WorkflowExecutor()
    
    input_data = {
        "transaction_id": "TX123",
        "amount": 50000,
        "risk_score": 0.85
    }
    
    # Execute workflow
    run = await executor.execute(
        workflow_id=sample_workflow.id,
        input_data=input_data,
        account_id="ACC123",
        db=db_session
    )
    
    # Assertions
    assert run is not None
    assert run.workflow_id == sample_workflow.id
    assert run.account_id == "ACC123"
    assert run.status is not None
    print(f"✓ Workflow executed successfully: Run ID {run.id}")


@pytest.mark.asyncio
async def test_workflow_replay(db_session: AsyncSession, sample_workflow: WorkflowDefinition):
    """Test workflow replay functionality."""
    executor = WorkflowExecutor()
    
    input_data = {
        "transaction_id": "TX456",
        "amount": 75000,
        "risk_score": 0.92
    }
    
    # Original execution
    original_run = await executor.execute(
        workflow_id=sample_workflow.id,
        input_data=input_data,
        account_id="ACC456",
        db=db_session
    )
    
    # Replay
    replay_run = await executor.replay(
        original_run_id=original_run.id,
        db=db_session
    )
    
    # Assertions
    assert replay_run is not None
    assert replay_run.id != original_run.id
    assert replay_run.replayed_from_run_id == original_run.id
    assert replay_run.input_data == original_run.input_data
    print(f"✓ Workflow replayed successfully: Replay Run ID {replay_run.id}")


@pytest.mark.asyncio
async def test_authentication_flow():
    """Test user registration and login flow."""
    import requests
    
    base_url = "http://127.0.0.1:8000/api/v1"
    
    # Register
    register_data = {
        "email": f"test_{uuid4()}@example.com",
        "password": "testpass123",
        "full_name": "Integration Test User"
    }
    
    response = requests.post(f"{base_url}/auth/register", json=register_data)
    assert response.status_code == 201
    user_data = response.json()
    assert user_data["email"] == register_data["email"]
    
    # Login
    login_data = {
        "username": register_data["email"],
        "password": register_data["password"]
    }
    
    response = requests.post(f"{base_url}/auth/login", data=login_data)
    assert response.status_code == 200
    token_data = response.json()
    assert "access_token" in token_data
    
    # Access protected endpoint
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    response = requests.get(f"{base_url}/auth/me", headers=headers)
    assert response.status_code == 200
    profile = response.json()
    assert profile["email"] == register_data["email"]
    
    print(f"✓ Authentication flow completed successfully")


if __name__ == "__main__":
    # Run tests manually
    print("Running Integration Tests...\n")
    
    print("=" * 60)
    print("TEST: Authentication Flow")
    print("=" * 60)
    asyncio.run(test_authentication_flow())
    
    print("\n" + "=" * 60)
    print("All integration tests would run with: pytest tests/test_integration.py")
    print("=" * 60)

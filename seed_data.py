"""
Create sample seed data for the system.
This includes users, roles, sample workflows, and documents.
"""
import asyncio
from uuid import uuid4
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session_maker
from app.core.security import get_password_hash
from app.models.user import User, Role, UserRole
from app.models.workflow import WorkflowDefinition, WorkflowStep, PromptTemplate, WorkflowStatus, StepType
from app.models.rule import Rule, RuleVersion


async def create_roles(db: AsyncSession):
    """Create standard roles."""
    roles_data = [
        {"name": "admin", "description": "System administrator with full access"},
        {"name": "author", "description": "Can create and edit draft workflows"},
        {"name": "approver", "description": "Can approve workflows for execution"},
        {"name": "executor", "description": "Can execute approved workflows"},
        {"name": "auditor", "description": "Can view audit logs and replay executions"}
    ]
    
    roles = []
    for role_data in roles_data:
        role = Role(
            id=uuid4(),
            name=role_data["name"],
            description=role_data["description"]
        )
        db.add(role)
        roles.append(role)
    
    await db.flush()
    return roles


async def create_users(db: AsyncSession, roles: list):
    """Create sample users."""
    users_data = [
        {
            "email": "admin@acmebank.com",
            "password": "admin123",
            "full_name": "Admin User",
            "role_names": ["admin", "author", "approver", "executor", "auditor"]
        },
        {
            "email": "analyst@acmebank.com",
            "password": "analyst123",
            "full_name": "Fraud Analyst",
            "role_names": ["author", "executor"]
        },
        {
            "email": "manager@acmebank.com",
            "password": "manager123",
            "full_name": "Risk Manager",
            "role_names": ["approver", "auditor"]
        }
    ]
    
    role_map = {role.name: role for role in roles}
    users = []
    
    for user_data in users_data:
        user = User(
            id=uuid4(),
            email=user_data["email"],
            hashed_password=get_password_hash(user_data["password"]),
            full_name=user_data["full_name"],
            is_active=True
        )
        db.add(user)
        await db.flush()
        
        # Assign roles
        for role_name in user_data["role_names"]:
            if role_name in role_map:
                user_role = UserRole(
                    id=uuid4(),
                    user_id=user.id,
                    role_id=role_map[role_name].id
                )
                db.add(user_role)
        
        users.append(user)
    
    return users


async def create_sample_rules(db: AsyncSession, admin_user_id):
    """Create sample rules."""
    rules_data = [
        {
            "name": "high_risk_threshold",
            "domain": "fraud",
            "description": "Escalate transactions with high risk scores",
            "logic": {
                "condition": {
                    "field": "risk_score",
                    "op": ">",
                    "value": 0.8
                },
                "result": "ESCALATE_TO_HUMAN"
            }
        },
        {
            "name": "high_value_check",
            "domain": "fraud",
            "description": "Require approval for high-value transactions",
            "logic": {
                "condition": {
                    "field": "amount",
                    "op": ">",
                    "value": 50000
                },
                "result": "REQUIRE_APPROVAL"
            }
        },
        {
            "name": "auto_approve_low_risk",
            "domain": "fraud",
            "description": "Auto-approve low-risk transactions",
            "logic": {
                "condition": {
                    "all": [
                        {"field": "risk_score", "op": "<", "value": 0.3},
                        {"field": "amount", "op": "<", "value": 10000}
                    ]
                },
                "result": "AUTO_APPROVE"
            }
        }
    ]
    
    for rule_data in rules_data:
        rule = Rule(
            id=uuid4(),
            name=rule_data["name"],
            domain=rule_data["domain"],
            description=rule_data["description"]
        )
        db.add(rule)
        await db.flush()
        
        # Create initial version
        rule_version = RuleVersion(
            id=uuid4(),
            rule_id=rule.id,
            version_number=1,
            logic_json=rule_data["logic"],
            is_active=True,
            created_by=admin_user_id
        )
        db.add(rule_version)


async def create_sample_workflows(db: AsyncSession, admin_user_id):
    """Create sample workflow definitions."""
    # Fraud Assessment Workflow
    fraud_workflow = WorkflowDefinition(
        id=uuid4(),
        name="Fraud Assessment Workflow",
        description="AI-assisted fraud detection for high-value transactions",
        domain="fraud",
        version=1,
        status=WorkflowStatus.APPROVED,
        is_active=True,
        created_by=admin_user_id,
        approved_by=admin_user_id,
        approved_at=datetime.utcnow()
    )
    db.add(fraud_workflow)
    await db.flush()
    
    # Steps for fraud workflow
    steps_data = [
        {
            "order_index": 0,
            "step_type": StepType.AI_ANALYSIS,
            "name": "Risk Scoring",
            "description": "AI analyzes transaction for fraud indicators",
            "config": {
                "rag_query": "fraud detection criteria and risk indicators",
                "prompt_template": "Analyze this transaction for fraud risk based on regulatory guidelines"
            }
        },
        {
            "order_index": 1,
            "step_type": StepType.DOMAIN_RULE,
            "name": "Risk Threshold Check",
            "description": "Apply deterministic risk thresholds",
            "config": {
                "rule_id": "high_risk_threshold"
            }
        },
        {
            "order_index": 2,
            "step_type": StepType.DOMAIN_RULE,
            "name": "High Value Check",
            "description": "Check transaction amount",
            "config": {
                "rule_id": "high_value_check"
            }
        },
        {
            "order_index": 3,
            "step_type": StepType.HUMAN_APPROVAL,
            "name": "Manual Review",
            "description": "Human reviews flagged transactions",
            "config": {}
        }
    ]
    
    for step_data in steps_data:
        step = WorkflowStep(
            id=uuid4(),
            workflow_id=fraud_workflow.id,
            order_index=step_data["order_index"],
            step_type=step_data["step_type"],
            name=step_data["name"],
            description=step_data["description"],
            config_json=step_data["config"]
        )
        db.add(step)


async def create_prompt_templates(db: AsyncSession):
    """Create reusable prompt templates."""
    templates = [
        {
            "name": "fraud_risk_analysis",
            "version": 1,
            "template_text": """You are a fraud detection expert analyzing a financial transaction.

Transaction Details:
{transaction_data}

Regulatory Context:
{rag_context}

Analyze this transaction and provide:
1. Risk score (0.0 to 1.0)
2. Key risk indicators found
3. Recommended action
4. Confidence level

Output as JSON.""",
            "input_schema": {
                "transaction_data": "object",
                "rag_context": "string"
            },
            "output_schema": {
                "risk_score": "number",
                "indicators": "array",
                "recommended_action": "string",
                "confidence": "number"
            }
        }
    ]
    
    for template_data in templates:
        template = PromptTemplate(
            id=uuid4(),
            name=template_data["name"],
            version=template_data["version"],
            template_text=template_data["template_text"],
            input_schema=template_data["input_schema"],
            output_schema=template_data["output_schema"],
            is_active=True
        )
        db.add(template)


async def main():
    """Main seed data creation function."""
    print("Creating seed data...")
    
    async_session_maker = get_session_maker()
    async with async_session_maker() as db:
        try:
            # Create roles
            print("  Creating roles...")
            roles = await create_roles(db)
            
            # Create users
            print("  Creating users...")
            users = await create_users(db, roles)
            admin_user = users[0]
            
            # Create rules
            print("  Creating sample rules...")
            await create_sample_rules(db, admin_user.id)
            
            # Create workflows
            print("  Creating sample workflows...")
            await create_sample_workflows(db, admin_user.id)
            
            # Create prompt templates
            print("  Creating prompt templates...")
            await create_prompt_templates(db)
            
            await db.commit()
            
            print("\n✓ Seed data created successfully!")
            print("\nSample Users:")
            print("  - admin@acmebank.com / admin123 (Full access)")
            print("  - analyst@acmebank.com / analyst123 (Author, Executor)")
            print("  - manager@acmebank.com / manager123 (Approver, Auditor)")
            
        except Exception as e:
            print(f"\n✗ Error creating seed data: {e}")
            await db.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(main())

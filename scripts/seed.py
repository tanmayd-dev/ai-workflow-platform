"""
Simplified seed data creation - minimal version to test.
"""
import sys
import os

# Add repository root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session_maker
from passlib.context import CryptContext
from app.models.user import User, Role, UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def main():
    """Create basic seed data."""
    print("Creating seed data...")
    
    async_session_maker = get_session_maker()
    async with async_session_maker() as db:
        try:
            # Create roles
            print("  Creating roles...")
            admin_role = Role(
                id=uuid4(),
                name="admin",
                description="System administrator"
            )
            author_role = Role(
                id=uuid4(),
                name="author",
                description="Workflow author"
            )
            db.add(admin_role)
            db.add(author_role)
            await db.flush()
            
            # Create admin user
            print("  Creating admin user...")
            admin_user = User(
                id=uuid4(),
                email="admin@acmebank.com",
                hashed_password=pwd_context.hash("admin123"),
                full_name="Admin User",
                is_active=True
            )
            db.add(admin_user)
            await db.flush()
            
            # Assign admin role
            print("  Assigning roles...")
            user_role = UserRole(
                id=uuid4(),
                user_id=admin_user.id,
                role_id=admin_role.id
            )
            db.add(user_role)
            
            await db.commit()
            
            print("\n✓ Seed data created successfully!")
            print("\nSample User:")
            print("  - admin@acmebank.com / admin123")
            
        except Exception as e:
            print(f"\n✗ Error creating seed data: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()
            raise

if __name__ == "__main__":
    asyncio.run(main())

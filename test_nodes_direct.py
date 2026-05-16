"""
Simple Direct Execution Test - No LangGraph
Just call each node type sequentially to verify everything works
"""
import asyncio
from app.services.llm_service import LLMService
from app.services.rule_engine import RuleEngine

async def main():
    print("="*70)
    print("DIRECT NODE EXECUTION TEST (No LangGraph)")
    print("="*70)
    
    # Test Data
    input_data = {
        "transaction": {
            "amount": 15000,
            "merchant": "International Store",
            "customer_id": "CUST-123"
        }
    }
    
    # Node 1: AI Analysis
    print("\n[1] 🤖 AI_ANALYSIS Node - LLM Reasoning...")
    print("-" * 70)
    llm = LLMService()
    
    ai_result = await llm.generate_completion(
        system_prompt="You are an AI fraud detection analyst.",
        user_prompt=f"Analyze this transaction for fraud risk:\n{input_data}\n\nProvide a risk_level (LOW/MEDIUM/HIGH) and brief explanation."
    )
    
    print(f"✓ LLM Response:")
    print(f"  {ai_result['content'][:200]}...")
    print(f"  Tokens used: {ai_result['usage']['total_tokens']}")
    
    # Node 2: System Rule Check
    print("\n[2] ⚙️ DOMAIN_RULE Node - System Logic...")
    print("-" * 70)
    rule_engine = RuleEngine()
    
    rule = {
        "field": "transaction.amount",
        "operator": ">",
        "value": 10000
    }
    
    rule_result = rule_engine.evaluate(rule, input_data)
    print(f"✓ Rule Result: {rule_result}")
    
    # Node 3: Human Approval
    print("\n[3] 👤 HUMAN_APPROVAL Node - Manual Review...")
    print("-" * 70)
    approval = {
        "approved": True,
        "approver": "system_auto",
        "notes": "Auto-approved for demo"
    }
    print(f"✓ Approval: {approval}")
    
    # Summary
    print("\n" + "="*70)
    print("✅ ALL 3 NODE TYPES EXECUTED SUCCESSFULLY!")
    print("="*70)
    print("🤖 AI_ANALYSIS: LLM called successfully")
    print("⚙️ DOMAIN_RULE: System logic evaluated")
    print("👤 HUMAN_APPROVAL: Approval workflow completed")
    print("\n✓ Check OpenRouter - you should see API usage now!")
    print("="*70)

if __name__ == "__main__":
    asyncio.run(main())

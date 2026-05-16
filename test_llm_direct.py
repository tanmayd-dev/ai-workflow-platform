"""
Direct ASYNC LLM Test
"""
import asyncio
import os
from dotenv import load_dotenv
from app.services.llm_service import LLMService

load_dotenv()

async def main():
    print("="*70)
    print("DIRECT ASYNC LLM TEST")
    print("="*70)
    
    api_key = os.getenv("OPENROUTER_API_KEY")
    print(f"✓ API Key: {api_key[:10]}...{api_key[-4:]}")
    
    llm = LLMService()
    
    print("\n[1] Calling OpenRouter API...")
    try:
        result = await llm.generate_completion(
            system_prompt="You are a helpful AI assistant.",
            user_prompt="Say 'Hello from AI Workflow!' and nothing else."
        )
        
        print(f"\n✅ SUCCESS! LLM Response:")
        print(f"  Content: {result['content']}")
        print(f"  Model: {result['model_name']}")
        print(f"  Tokens: {result['usage']['total_tokens']}")
        print(f"\n✓ Check OpenRouter dashboard - you should see 1 API call!")
        
    except Exception as e:
        print(f"\n✗ Failed: {e}")
        import traceback
        traceback.print_exc()
    
    print("="*70)

if __name__ == "__main__":
    asyncio.run(main())

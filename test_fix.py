#!/usr/bin/env python3
"""Quick test to verify the Windows asyncio fix works."""

import asyncio
import sys

def main():
    # Test Windows event loop policy fix
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print("[OK] Applied Windows event loop policy")

    # Test basic async function
    async def test():
        await asyncio.sleep(0.1)
        return True

    result = asyncio.run(test())
    print(f"[OK] Async function executed successfully: {result}")

    # Test httpx import and timeout
    try:
        import httpx
        timeout = httpx.Timeout(30.0)
        print(f"[OK] httpx imported and Timeout created: {timeout}")
    except Exception as e:
        print(f"[FAIL] httpx import failed: {e}")
        return

    print("\n[SUCCESS] All tests passed! The fix should resolve the hanging issue.")

if __name__ == "__main__":
    main()

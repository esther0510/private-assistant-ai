import sys

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        from personal_ai_assistant.friend_smoke import main
    else:
        from personal_ai_assistant.app import main
    raise SystemExit(main())

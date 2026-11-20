import multiprocessing


if __name__ == "__main__":
    # PyInstaller's child-process command line must be intercepted before the
    # CLI is imported, or a session-scan worker starts another full CLI.
    multiprocessing.freeze_support()

from codexsync.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

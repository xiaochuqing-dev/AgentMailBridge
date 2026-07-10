"""命令行入口：支持 `python -m agent_mail_bridge <command>`。"""

import sys

from agent_mail_bridge.cli import main

if __name__ == "__main__":
    sys.exit(main())

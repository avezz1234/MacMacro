#!/usr/bin/env python3
# XTernal for Mac -- Python entry point (use ./xternal-mac or the .command files).
# SPDX-License-Identifier: AGPL-3.0-only
# Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.
import os
import sys

# The macro runs as root; any .pyc it wrote here would be root-owned and block
# the updater (which runs as the normal user) from replacing files.
sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xternal.cli import main

if __name__ == "__main__":
    sys.exit(main())

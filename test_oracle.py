"""Compatibility redirect for test runners pointing to root test_oracle.py."""
import os
import sys

root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from tests.test_oracle import *

if __name__ == "__main__":
    import unittest
    unittest.main()

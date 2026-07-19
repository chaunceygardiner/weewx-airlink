# Make `import user.airlink` work when pytest is run from the repo root
# without setting PYTHONPATH=bin.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin'))

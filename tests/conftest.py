import os
import tempfile

# server.app을 import하기 전에: 데모 모드 + 임시 개인 DB
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

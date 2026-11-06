"""Workspace and capability transaction checks on the disposable MySQL CI."""
import os
import unittest

from MaiLivingWorld.tests import test_activity_mysql_live as activity
from MaiLivingWorld.tests.test_cognitive_workspace import WorkspaceChecks


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class WorkspaceMySQLLiveTests(WorkspaceChecks, unittest.TestCase):
    setUp = activity.ActivityMySQLLiveTests.setUp
    tearDown = activity.ActivityMySQLLiveTests.tearDown
    open_store = activity.ActivityMySQLLiveTests.open_store

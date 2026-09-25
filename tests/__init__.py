import logging

# The suite provokes failures on purpose -- dead serial links, stale clocks --
# and the collector logs every one. A handler on the root logger stops
# logging's last-resort handler printing them over the test report;
# assertLogs still captures each record it is waiting for.
logging.getLogger().addHandler(logging.NullHandler())

import pytest

from plenum.common.eventually import eventually
from plenum.common.types import Nomination

whitelist = ['already got nomination',
             'doing nothing for now']


# noinspection PyIncorrectDocstring,PyUnusedLocal,PyShadowingNames
@pytest.mark.skip(reason="SOV-540. Implementation changed.")
def testBlacklistNodeOnMultipleNominations(looper, keySharedNodes, ready):
    """
    A node that sends multiple nominations must be blacklisted by other nodes
    """
    nodeSet = keySharedNodes
    A, B, C, D = nodeSet.nodes.values()

    # B sends more than 2 nominations
    for i in range(3):
        B.send(Nomination(D.name, 0, B.viewNo))

    # B should be blacklisted by A, C, D
    def chk():
        for node in A, C, D:
            assert node.isNodeBlacklisted(B.name)

    # TODO[slow-factor]: add expectedNominationTimeout
    looper.run(eventually(chk, retryWait=1, timeout=3))

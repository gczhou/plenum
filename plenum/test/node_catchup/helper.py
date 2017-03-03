from typing import Iterable

from plenum.common.eventually import eventually
from plenum.common.types import HA
from plenum.test.helper import checkLedgerEquality
from plenum.test.test_client import TestClient
from plenum.test.test_node import TestNode
from plenum.test import waits
from plenum.common import util

# TODO: This should just take an arbitrary number of nodes and check for their
#  ledgers to be equal
def checkNodeLedgersForEquality(node: TestNode,
                                *otherNodes: Iterable[TestNode]):
    for n in otherNodes:
        checkLedgerEquality(node.domainLedger, n.domainLedger)
        checkLedgerEquality(node.poolLedger, n.poolLedger)


def ensureNewNodeConnectedClient(looper, client: TestClient, node: TestNode):
    stackParams = node.clientStackParams
    client.nodeReg[stackParams['name']] = HA('127.0.0.1', stackParams['ha'][1])
    looper.run(client.ensureConnectedToNodes())


def checkClientPoolLedgerSameAsNodes(client: TestClient,
                                     *nodes: Iterable[TestNode]):
    for n in nodes:
        checkLedgerEquality(client.ledger, n.poolLedger)


def ensureClientConnectedToNodesAndPoolLedgerSame(looper,
                                                  client: TestClient,
                                                  *nodes:Iterable[TestNode]):
    fVal = util.getMaxFailures(len(nodes))
    poolCheckTimeout = waits.expectedPoolLedgerCheck(fVal)
    looper.run(eventually(checkClientPoolLedgerSameAsNodes,
                          client,
                          *nodes,
                          timeout=poolCheckTimeout))
    looper.run(client.ensureConnectedToNodes())

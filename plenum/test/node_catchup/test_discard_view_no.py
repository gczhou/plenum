import time
from functools import partial

import pytest

from plenum.common.eventually import eventually
from plenum.common.types import Nomination, PrePrepare
from plenum.common.util import randomString
from plenum.test.delayers import delayNonPrimaries
from plenum.test.helper import sendReqsToNodesAndVerifySuffReplies, \
    waitForViewChange, checkDiscardMsg
from plenum.test.node_catchup.helper import checkNodeLedgersForEquality
from plenum.test.pool_transactions.helper import addNewStewardAndNode
from plenum.test.test_node import checkNodesConnected, \
    checkProtocolInstanceSetup

whitelist = ['found legacy entry']  # warnings


@pytest.mark.skip(reason='SOV-456')
def testNodeDiscardMessageFromUnknownView(txnPoolNodeSet,
                                          nodeSetWithNodeAddedAfterSomeTxns,
                                          newNodeCaughtUp, tdirWithPoolTxns,
                                          tconf, allPluginsPath):
    """
    Node discards 3-phase and election messages from view nos that it does not
    know of (view nos before it joined the pool)
    :return:
    """
    looper, nodeX, client, wallet, _, _ = nodeSetWithNodeAddedAfterSomeTxns
    viewNo = nodeX.viewNo

    # Delay processing of PRE-PREPARE from all non primary replicas of master
    # so master's performance falls and view changes
    delayNonPrimaries(txnPoolNodeSet, 0, 10)
    sendReqsToNodesAndVerifySuffReplies(looper, wallet, client, 4)
    waitForViewChange(looper, txnPoolNodeSet, expectedViewNo=viewNo+1)

    newStewardName = "testClientSteward" + randomString(3)
    nodeName = "Theta"
    _, _, nodeTheta = addNewStewardAndNode(looper, client,
                                           wallet,
                                           newStewardName,
                                           nodeName,
                                           tdirWithPoolTxns, tconf,
                                           allPluginsPath)
    txnPoolNodeSet.append(nodeTheta)
    looper.run(checkNodesConnected(txnPoolNodeSet))
    looper.run(client.ensureConnectedToNodes())
    looper.run(eventually(checkNodeLedgersForEquality, nodeTheta,
                          *txnPoolNodeSet[:-1], retryWait=1, timeout=5))
    checkProtocolInstanceSetup(looper, txnPoolNodeSet, retryWait=1,
                               timeout=10)
    electMsg = Nomination(nodeX.name, 0, viewNo)
    threePMsg = PrePrepare(
            0,
            viewNo,
            10,
            wallet.defaultId,
            wallet._getIdData().lastReqId+1,
            "random digest",
            time.time()
            )
    ridTheta = nodeX.nodestack.getRemote(nodeTheta.name).uid
    nodeX.send(electMsg, ridTheta)
    nodeX.send(threePMsg, ridTheta)
    nodeX.send(electMsg, ridTheta)

    from plenum.test import waits
    timeout = waits.expectedNodeToNodeMessageDeliveryTime()

    looper.run(eventually(checkDiscardMsg, [nodeTheta, ], electMsg,
                          'un-acceptable viewNo', retryWait=1, timeout=timeout))
    nodeX.send(threePMsg, ridTheta)
    looper.run(eventually(checkDiscardMsg, [nodeTheta, ], threePMsg,
                          'un-acceptable viewNo', retryWait=1, timeout=timeout))

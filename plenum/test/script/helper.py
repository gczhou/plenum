import filecmp
import os

import pytest

from plenum.client.wallet import Wallet
from plenum.common.eventually import eventually
from plenum.common.log import getlogger
from plenum.common.port_dispenser import genHa
from plenum.common.script_helper import changeHA
from plenum.common.signer_simple import SimpleSigner
from plenum.common.util import getMaxFailures
from plenum.test.helper import checkSufficientRepliesReceived, \
    sendReqsToNodesAndVerifySuffReplies
from plenum.test.test_client import genTestClient
from plenum.test.test_node import TestNode, checkNodesConnected, \
    ensureElectionsDone

logger = getlogger()


@pytest.fixture(scope="module")
def tconf(tconf, request):
    oldVal = tconf.UpdateGenesisPoolTxnFile
    tconf.UpdateGenesisPoolTxnFile = True

    def reset():
        tconf.UpdateGenesisPoolTxnFile = oldVal

    request.addfinalizer(reset)
    return tconf


@pytest.yield_fixture(scope="module")
def looper(txnPoolNodesLooper):
    yield txnPoolNodesLooper


def checkIfGenesisPoolTxnFileUpdated(*nodesAndClients):
    for item in nodesAndClients:
        poolTxnFileName = item.poolManager.ledgerFile if \
            isinstance(item, TestNode) else item.ledgerFile
        genFile = os.path.join(item.basedirpath, poolTxnFileName)
        ledgerFile = os.path.join(item.dataLocation, poolTxnFileName)
        assert filecmp.cmp(genFile, ledgerFile, shallow=False)


def changeNodeHa(looper, txnPoolNodeSet, tdirWithPoolTxns,
                 poolTxnData, poolTxnStewardNames, tconf, shouldBePrimary):

    # prepare new ha for node and client stack
    subjectedNode = None
    stewardName = None
    stewardsSeed = None

    for nodeIndex, n in enumerate(txnPoolNodeSet):
        if (shouldBePrimary and n.primaryReplicaNo == 0) or \
                (not shouldBePrimary and n.primaryReplicaNo != 0):
            subjectedNode = n
            stewardName = poolTxnStewardNames[nodeIndex]
            stewardsSeed = poolTxnData["seeds"][stewardName].encode()
            break

    nodeStackNewHA, clientStackNewHA = genHa(2)
    logger.debug("change HA for node: {} to {}".
                 format(subjectedNode.name, (nodeStackNewHA, clientStackNewHA)))

    nodeSeed = poolTxnData["seeds"][subjectedNode.name].encode()

    # change HA
    stewardClient, req = changeHA(looper, tconf, subjectedNode.name, nodeSeed,
                                  nodeStackNewHA, stewardName, stewardsSeed)
    f = getMaxFailures(len(stewardClient.nodeReg))
    looper.run(eventually(checkSufficientRepliesReceived, stewardClient.inBox,
                          req.reqId, f, retryWait=1, timeout=20))

    # stop node for which HA will be changed
    subjectedNode.stop()
    looper.removeProdable(subjectedNode)

    # start node with new HA
    restartedNode = TestNode(subjectedNode.name, basedirpath=tdirWithPoolTxns,
                             config=tconf, ha=nodeStackNewHA,
                             cliha=clientStackNewHA)
    looper.add(restartedNode)

    txnPoolNodeSet[nodeIndex] = restartedNode
    looper.run(checkNodesConnected(txnPoolNodeSet, overrideTimeout=70))
    ensureElectionsDone(looper, txnPoolNodeSet)

    # start client and check the node HA
    anotherClient, _ = genTestClient(tmpdir=tdirWithPoolTxns,
                                     usePoolLedger=True)
    looper.add(anotherClient)
    looper.run(eventually(anotherClient.ensureConnectedToNodes))
    stewardWallet = Wallet(stewardName)
    stewardWallet.addIdentifier(signer=SimpleSigner(seed=stewardsSeed))
    sendReqsToNodesAndVerifySuffReplies(looper, stewardWallet, stewardClient, 8)
    looper.run(eventually(checkIfGenesisPoolTxnFileUpdated, *txnPoolNodeSet,
                          stewardClient, anotherClient, retryWait=1,
                          timeout=10))

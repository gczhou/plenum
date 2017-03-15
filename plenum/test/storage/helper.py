from plenum.common.eventually import eventually
from plenum.common.txn import TXN_TYPE
from plenum.common.types import f
from plenum.test import waits


def checkReplyIsPersisted(nodes, lpr, reply1):
    def chk(node):
        result = node.domainLedger.get(identifier=reply1.identifier,
                                       reqId=reply1.reqId)
        assert result.get(f.REQ_ID.nm) == reply1.reqId
        assert result.get(f.IDENTIFIER.nm) == reply1.identifier
        assert result.get(TXN_TYPE) == reply1.operation.get(TXN_TYPE)

    timeout = waits.expectedPoolLedgerCheck(len(nodes))
    for node in nodes:
        lpr.run(eventually(chk, node, retryWait=1, timeout=timeout))

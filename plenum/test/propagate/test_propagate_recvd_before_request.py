import pytest

from plenum.common.eventually import eventually
from plenum.common.types import Propagate
from plenum.test.delayers import delay
from plenum.test.helper import assertLength
from plenum.test.propagate.helper import recvdRequest, recvdPropagate, \
    sentPropagate, forwardedRequest
from plenum.test import waits


nodeCount = 4


@pytest.fixture()
def setup(nodeSet):
    A, B, C, D = nodeSet.nodes.values()
    A.clientIbStasher.delay(lambda x: 5)
    delay(Propagate, frm=[C, D], to=A, howlong=10)


def testPropagateRecvdBeforeRequest(setup, looper, nodeSet, up, sent1):
    A, B, C, D = nodeSet.nodes.values()

    def x():
        # A should not have received a request from the client
        assert len(recvdRequest(A)) == 0
        # A should have received only one PROPAGATE
        assert len(recvdPropagate(A)) == 1
        # A should have sent only one PROPAGATE
        assert len(sentPropagate(A)) == 1

    looper.run(eventually(x, retryWait=.5, timeout=3))

    def y():
        # A should have received a request from the client
        assert len(recvdRequest(A)) == 1
        # A should still have sent only one PROPAGATE
        assert len(sentPropagate(A)) == 1

    looper.run(eventually(y, retryWait=.5, timeout=6))

    def chk():
        # A should have forwarded the request
        assertLength(forwardedRequest(A), 1)

    timeout = waits.expectedClientRequestPropagationTime(len(nodeSet))
    looper.run(eventually(chk, retryWait=1, timeout=timeout))

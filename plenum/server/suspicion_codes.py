from typing import NamedTuple
import inspect


Suspicion = NamedTuple("SuspicionCode", [("code", int), ("reason", str)])


class Suspicions:
    PPR_TO_PRIMARY = \
        Suspicion(1, "PRE-PREPARE being sent to primary")
    DUPLICATE_PPR_SENT = \
        Suspicion(2,
                  "PRE-PREPARE being sent twice with the same view no and "
                  "sequence no")
    DUPLICATE_PR_SENT = \
        Suspicion(3, "PREPARE request already received")
    UNKNOWN_PR_SENT = \
        Suspicion(4, "PREPARE request for unknown PRE-PREPARE request")
    PR_DIGEST_WRONG = \
        Suspicion(5, "PREPARE request digest is incorrect")
    UNKNOWN_CM_SENT = \
        Suspicion(6, "Commit requests when no prepares received")
    CM_DIGEST_WRONG = \
        Suspicion(7, "Commit requests has incorrect digest")
    DUPLICATE_CM_SENT = \
        Suspicion(8, "COMMIT message has already received")
    PPR_FRM_NON_PRIMARY = \
        Suspicion(9, "Pre-Prepare received from non primary")
    PR_FRM_PRIMARY = \
        Suspicion(10, "Prepare received from primary")
    PPR_DIGEST_WRONG = \
        Suspicion(11, "Pre-Prepare message has incorrect digest")
    DUPLICATE_INST_CHNG = \
        Suspicion(12, "Duplicate instance change message received")
    FREQUENT_INST_CHNG = \
        Suspicion(13, "Too many instance change messages received")
    DUPLICATE_NOM_SENT = \
        Suspicion(14, "NOMINATION request already received")
    DUPLICATE_PRI_SENT = \
        Suspicion(15, "PRIMARY request already received")
    DUPLICATE_REL_SENT = \
        Suspicion(16, "REELECTION request already received")
    WRONG_PPSEQ_NO = \
        Suspicion(17, "Wrong PRE-PREPARE seq number")
    PR_TIME_WRONG = \
        Suspicion(5, "PREPARE time does not match with PRE-PREPARE")
    CM_TIME_WRONG = \
        Suspicion(5, "COMMIT time does not match with PRE-PREPARE")

    @classmethod
    def getList(cls):
        return [member for nm, member in inspect.getmembers(cls) if isinstance(
            member, Suspicion)]

import math
import random
import time
from collections import Counter, deque
from functools import partial
from typing import Sequence, Any, Union, List

from plenum.common.types import Nomination, Reelection, Primary, f
from plenum.common.util import mostCommonElement, getQuorum
from plenum.common.log import getlogger
from plenum.server import replica
from plenum.server.primary_decider import PrimaryDecider
from plenum.server.router import Router


logger = getlogger()


# The elector should not blacklist nodes if it receives multiple nominations
# or primary or re-election messages, until there are roo many (over 50 maybe)
# duplicate messages. Consider a case where a node say Alpha, took part in
# election and election completed and soon after that Alpha crashed. Now Alpha
#  comes back up and receives Nominations and Primary. Now Alpha will react to
#  that and send Nominations or Primary, which will lead to it being
# blacklisted. Maybe Alpha should not react to Nomination or Primary it gets
# for elections it was not part of. Elections need to have round numbers.


class PrimaryElector(PrimaryDecider):
    """
    Responsible for managing the election of a primary for all instances for
    a particular Node. Each node has a PrimaryElector.
    """

    def __init__(self, node):
        super().__init__(node)

        # TODO: How does primary decider ensure that a node does not have a
        # primary while its catching up
        self.node = node

        self.replicaNominatedForItself = None
        """Flag variable which indicates which replica has nominated
        for itself"""

        self.nominations = {}

        self.primaryDeclarations = {}

        self.scheduledPrimaryDecisions = {}

        self.reElectionProposals = {}

        self.reElectionRounds = {}

        routerArgs = [(Nomination, self.processNominate),
                      (Primary, self.processPrimary),
                      (Reelection, self.processReelection)]
        self.inBoxRouter = Router(*routerArgs)

        self.pendingMsgsForViews = {}  # Dict[int, deque]

        # Keeps track of duplicate messages received. Used to blacklist if
        # nodes send more than 1 duplicate messages. Useful to blacklist
        # nodes. This number `1` is configurable. The reason 1 duplicate
        # message is tolerated is because sometimes when a node communicates
        # to an already lagged node, an extra NOMINATE or PRIMARY might be sent
        self.duplicateMsgs = {}   # Dict[Tuple, int]

    def __repr__(self):
        return "{}".format(self.name)

    @property
    def hasPrimaryReplica(self) -> bool:
        """
        Return whether this node has a primary replica.
        """
        return any([r.isPrimary for r in self.replicas])

    def setDefaults(self, instId: int):
        """
        Set the default values for elections for a replica.

        :param instId: instance id
        """
        logger.debug(
            "{} preparing replica with instId {}".format(self.name, instId))
        self.reElectionRounds[instId] = 0
        self.setElectionDefaults(instId)

    def prepareReplicaForElection(self, replica: 'replica.Replica'):
        """
        Prepare the replica state to get ready for elections.

        :param replica: the replica to prepare for elections
        """
        instId = replica.instId
        if instId not in self.nominations:
            self.setDefaults(instId)

    def pendMsgForLaterView(self, msg: Any, viewNo: int):
        """
        Add a message to the pending queue for a later view.

        :param msg: the message to pend
        :param viewNo: the viewNo this message is meant for.
        """
        if viewNo not in self.pendingMsgsForViews:
            self.pendingMsgsForViews[viewNo] = deque()
        self.pendingMsgsForViews[viewNo].append(msg)

    def filterMsgs(self, wrappedMsgs: deque) -> deque:
        """
        Filters messages by view number so that only the messages that have the
        current view number are retained.

        :param wrappedMsgs: the messages to filter
        """
        filtered = deque()
        while wrappedMsgs:
            wrappedMsg = wrappedMsgs.popleft()
            msg, sender = wrappedMsg
            if hasattr(msg, f.VIEW_NO.nm):
                reqViewNo = getattr(msg, f.VIEW_NO.nm)
                if reqViewNo == self.viewNo:
                    filtered.append(wrappedMsg)
                elif reqViewNo > self.viewNo:
                    logger.debug(
                        "{}'s elector queueing {} since it is for a later view"
                            .format(self.name, wrappedMsg))
                    self.pendMsgForLaterView((msg, sender), reqViewNo)
                else:
                    self.discard(wrappedMsg,
                                 "its view no {} is less than the elector's {}"
                                 .format(reqViewNo, self.viewNo),
                                 logger.debug)
            else:
                filtered.append(wrappedMsg)

        return filtered

    def didReplicaNominate(self, instId: int):
        """
        Return whether this replica nominated a candidate for election

        :param instId: the instance id (used to identify the replica on this node)
        """
        return instId in self.nominations and \
            self.replicas[instId].name in self.nominations[instId]

    def didReplicaDeclarePrimary(self, instId: int):
        """
        Return whether this replica a candidate as primary for election

        :param instId: the instance id (used to identify the replica on this node)
        """
        return instId in self.primaryDeclarations and \
               self.replicas[instId].name in self.primaryDeclarations[instId]

    async def serviceQueues(self, limit=None):
        """
        Service at most `limit` messages from the inBox.

        :param limit: the maximum number of messages to service
        :return: the number of messages successfully processed
        """
        return await self.inBoxRouter.handleAll(self.filterMsgs(self.inBox),
                                                limit)

    @property
    def quorum(self) -> int:
        r"""
        Return the quorum of this RBFT system. Equal to :math:`2f + 1`.
        """
        return getQuorum(f=self.f)

    def decidePrimaries(self):  # overridden method of PrimaryDecider
        self.scheduleElection()

    def scheduleElection(self):
        """
        Schedule election at some time in the future. Currently the election
        starts immediately.
        """
        self._schedule(self.startElection)

    def startElection(self):
        """
        Start the election process by nominating self as primary.
        """
        logger.debug("{} starting election".format(self))
        for r in self.replicas:
            self.prepareReplicaForElection(r)

        self.nominateItself()

    def nominateItself(self):
        """
        Actions to perform if this node hasn't nominated any of its replicas.
        """
        if self.replicaNominatedForItself is None:
            # If does not have a primary replica then nominate a replica
            if not self.hasPrimaryReplica:
                logger.debug(
                    "{} attempting to nominate a replica".format(self.name))
                self.nominateRandomReplica()
            else:
                logger.debug(
                    "{} already has a primary replica".format(self.name))
        else:
            logger.debug(
                "{} already has an election in progress".format(self.name))

    def nominateRandomReplica(self):
        """
        Randomly nominate one of the replicas on this node (for which elections
        aren't yet completed) as primary.
        """
        if not self.node.isParticipating:
            logger.debug("{} cannot nominate a replica yet since catching up"
                         .format(self))
            return

        undecideds = [i for i, r in enumerate(self.replicas)
                      if r.isPrimary is None]
        if undecideds:
            chosen = random.choice(undecideds)
            logger.debug("Node {} does not have a primary, "
                         "replicas {} are undecided, "
                         "choosing {} to nominate".
                         format(self, undecideds, chosen))

            # A replica has nominated for itself, so set the flag
            self.replicaNominatedForItself = chosen
            self._schedule(partial(self.nominateReplica, chosen))
        else:
            logger.debug("Node {} does not have a primary, "
                         "but elections for all {} instances "
                         "have been decided".
                         format(self, len(self.replicas)))

    def nominateReplica(self, instId):
        """
        Nominate the replica identified by `instId` on this node as primary.
        """
        replica = self.replicas[instId]
        if not self.didReplicaNominate(instId):
            self.nominations[instId][replica.name] = replica.name
            logger.info("{} nominating itself for instance {}".
                        format(replica, instId),
                        extra={"cli": "PLAIN", "tags": ["node-nomination"]})
            self.sendNomination(replica.name, instId, self.viewNo)
        else:
            logger.debug(
                "{} already nominated, so hanging back".format(replica))

    # noinspection PyAttributeOutsideInit
    def setElectionDefaults(self, instId):
        """
        Set defaults for parameters used in the election process.
        """
        self.nominations[instId] = {}
        self.primaryDeclarations[instId] = {}
        self.scheduledPrimaryDecisions[instId] = None
        self.reElectionProposals[instId] = {}
        self.duplicateMsgs = {}

    def processNominate(self, nom: Nomination, sender: str):
        """
        Process a Nomination message.

        :param nom: the nomination message
        :param sender: sender address of the nomination
        """
        logger.debug("{}'s elector started processing nominate msg: {}".
                     format(self.name, nom))
        instId = nom.instId
        replica = self.replicas[instId]
        sndrRep = replica.generateName(sender, nom.instId)

        if not self.didReplicaNominate(instId):
            if instId not in self.nominations:
                self.setDefaults(instId)
            self.nominations[instId][replica.name] = nom.name
            self.sendNomination(nom.name, nom.instId, nom.viewNo)
            logger.debug("{} nominating {} for instance {}".
                         format(replica, nom.name, nom.instId),
                         extra={"cli": "PLAIN", "tags": ["node-nomination"]})

        else:
            logger.debug("{} already nominated".format(replica.name))

        # Nodes should not be able to vote more than once
        if sndrRep not in self.nominations[instId]:
            self.nominations[instId][sndrRep] = nom.name
            logger.debug("{} attempting to decide primary based on nomination "
                         "request: {} from {}".format(replica, nom, sndrRep))
            self._schedule(partial(self.decidePrimary, instId))
        else:
            self.discard(nom,
                         "already got nomination from {}".
                         format(sndrRep),
                         logger.warning)

            key = (Nomination.typename, instId, sndrRep)
            self.duplicateMsgs[key] = self.duplicateMsgs.get(key, 0) + 1

            # If got more than one duplicate message then blacklist
            # if self.duplicateMsgs[key] > 1:
            #     self.send(BlacklistMsg(Suspicions.DUPLICATE_NOM_SENT.code, sender))

    def processPrimary(self, prim: Primary, sender: str) -> None:
        """
        Process a vote from a replica to select a particular replica as primary.
        Once 2f + 1 primary declarations have been received, decide on a primary replica.

        :param prim: a vote
        :param sender: the name of the node from which this message was sent
        """
        logger.debug("{}'s elector started processing primary msg from {} : {}"
                     .format(self.name, sender, prim))
        instId = prim.instId
        replica = self.replicas[instId]
        sndrRep = replica.generateName(sender, prim.instId)

        # Nodes should not be able to declare `Primary` winner more than more
        if instId not in self.primaryDeclarations:
            self.setDefaults(instId)
        if sndrRep not in self.primaryDeclarations[instId]:
            self.primaryDeclarations[instId][sndrRep] = prim.name

            # If got more than 2f+1 primary declarations then in a position to
            # decide whether it is the primary or not `2f + 1` declarations
            # are enough because even when all the `f` malicious nodes declare
            # a primary, we still have f+1 primary declarations from
            # non-malicious nodes. One more assumption is that all the non
            # malicious nodes vote for the the same primary

            # Find for which node there are maximum primary declarations.
            # Cant be a tie among 2 nodes since all the non malicious nodes
            # which would be greater than or equal to f+1 would vote for the
            # same node

            if replica.isPrimary is not None:
                logger.debug(
                    "{} Primary already selected; ignoring PRIMARY msg".format(
                        replica))
                return

            if self.hasPrimaryQuorum(instId):
                if replica.isPrimary is None:
                    primary = mostCommonElement(
                        self.primaryDeclarations[instId].values())
                    logger.display("{} selected primary {} for instance {} "
                                   "(view {})".format(replica, primary,
                                                      instId, self.viewNo),
                                   extra={"cli": "ANNOUNCE",
                                          "tags": ["node-election"]})
                    logger.debug("{} selected primary on the basis of {}".
                                 format(replica,
                                        self.primaryDeclarations[instId]),
                                 extra={"cli": False})

                    # If the maximum primary declarations are for this node
                    # then make it primary
                    replica.primaryName = primary

                    # If this replica has nominated itself and since the
                    # election is over, reset the flag
                    if self.replicaNominatedForItself == instId:
                        self.replicaNominatedForItself = None

                    self.node.primaryFound()

                    self.scheduleElection()
                else:
                    self.discard(prim,
                                 "it already decided primary which is {}".
                                 format(replica.primaryName),
                                 logger.debug)
            else:
                logger.debug(
                    "{} received {} but does it not have primary quorum yet"
                        .format(self.name, prim))
        else:
            self.discard(prim,
                         "already got primary declaration from {}".
                         format(sndrRep),
                         logger.warning)

            key = (Primary.typename, instId, sndrRep)
            self.duplicateMsgs[key] = self.duplicateMsgs.get(key, 0) + 1
            # If got more than one duplicate message then blacklist
            # if self.duplicateMsgs[key] > 1:
            #     self.send(BlacklistMsg(
            #         Suspicions.DUPLICATE_PRI_SENT.code, sender))

    def processReelection(self, reelection: Reelection, sender: str):
        """
        Process reelection requests sent by other nodes.
        If quorum is achieved, proceed with the reelection process.

        :param reelection: the reelection request
        :param sender: name of the  node from which the reelection was sent
        """
        logger.debug(
            "{}'s elector started processing reelection msg".format(self.name))
        # Check for election round number to discard any previous
        # reelection round message
        instId = reelection.instId
        replica = self.replicas[instId]
        sndrRep = replica.generateName(sender, reelection.instId)

        if instId not in self.reElectionProposals:
            self.setDefaults(instId)

        expectedRoundDiff = 0 if (replica.name in
                                  self.reElectionProposals[instId]) else 1
        expectedRound = self.reElectionRounds[instId] + expectedRoundDiff

        if not reelection.round == expectedRound:
            self.discard(reelection,
                         "reelection request from {} with round "
                         "number {} does not match expected {}".
                         format(sndrRep, reelection.round, expectedRound),
                         logger.debug)
            return

        if sndrRep not in self.reElectionProposals[instId]:
            self.reElectionProposals[instId][sndrRep] = reelection.tieAmong

            # Check if got reelection messages from at least 2f + 1 nodes (1
            # more than max faulty nodes). Necessary because some nodes may
            # turn out to be malicious and send re-election frequently

            if self.hasReelectionQuorum(instId):
                logger.debug("{} achieved reelection quorum".format(replica),
                             extra={"cli": True})
                # Need to find the most frequent tie reported to avoid `tie`s
                # from malicious nodes. Since lists are not hashable so
                # converting each tie(a list of node names) to a tuple.
                ties = [tuple(t) for t in
                        self.reElectionProposals[instId].values()]
                tieAmong = mostCommonElement(ties)

                self.setElectionDefaults(instId)

                # There was a tie among this and some other node(s), so do a
                # random wait
                if replica.name in tieAmong:
                    # Try to nominate self after a random delay but dont block
                    # until that delay and because a nominate from another
                    # node might be sent
                    self._schedule(partial(self.nominateReplica, instId),
                                   random.randint(1, 3))
                else:
                    # Now try to nominate self again as there is a reelection
                    self.nominateReplica(instId)
            else:
                logger.debug(
                    "{} does not have re-election quorum yet. Got only {}".format(
                        replica, len(self.reElectionProposals[instId])))
        else:
            self.discard(reelection,
                         "already got re-election proposal from {}".
                         format(sndrRep),
                         logger.warning)

    def hasReelectionQuorum(self, instId: int) -> bool:
        """
        Are there at least `quorum` number of reelection requests received by
        this replica?

        :return: True if number of reelection requests is greater than quorum,
        False otherwise
        """
        return len(self.reElectionProposals[instId]) >= self.quorum

    def hasNominationQuorum(self, instId: int) -> bool:
        """
        Are there at least `quorum` number of nominations received by this
        replica?

        :return: True if number of nominations is greater than quorum,
        False otherwise
        """
        return len(self.nominations[instId]) >= self.quorum

    def hasPrimaryQuorum(self, instId: int) -> bool:
        """
        Are there at least `quorum` number of primary declarations received by
        this replica?

        :return: True if number of primary declarations is greater than quorum,
         False otherwise
        """
        pd = len(self.primaryDeclarations[instId])
        q = self.quorum
        result = pd >= q
        if result:
            logger.trace("{} primary declarations {} meet required quorum {} "
                         "for instance id {}".format(self.node.replicas[instId],
                                                     pd, q, instId))
        return result

    def hasNominationsFromAll(self, instId: int) -> bool:
        """
        Did this replica receive nominations from all the replicas in the system?

        :return: True if this replica has received nominations from all replicas
        , False otherwise
        """
        return len(self.nominations[instId]) == self.nodeCount

    def decidePrimary(self, instId: int):
        """
        Decide which one among the nominated candidates can be a primary replica.
        Refer to the documentation on the election process for more details.
        """
        # Waiting for 2f+1 votes since at the most f nodes are malicious then
        # 2f+1 nodes have to be good. Not waiting for all nodes because some
        # nodes may turn out to be malicious and not vote at all

        replica = self.replicas[instId]

        if instId not in self.primaryDeclarations:
            self.primaryDeclarations[instId] = {}
        if instId not in self.reElectionProposals:
            self.reElectionProposals[instId] = {}
        if instId not in self.scheduledPrimaryDecisions:
            self.scheduledPrimaryDecisions[instId] = {}
        if instId not in self.reElectionRounds:
            self.reElectionRounds[instId] = 0

        if replica.name in self.primaryDeclarations[instId]:
            logger.debug("{} has already sent a Primary: {}".
                         format(replica,
                                self.primaryDeclarations[instId][replica.name]))
            return

        if replica.name in self.reElectionProposals[instId]:
            logger.debug("{} has already sent a Re-Election for : {}".
                         format(replica,
                                self.reElectionProposals[instId][replica.name]))
            return

        if self.hasNominationQuorum(instId):
            logger.debug("{} has got nomination quorum now".format(replica))
            primaryCandidates = self.getPrimaryCandidates(instId)

            # In case of one clear winner
            if len(primaryCandidates) == 1:
                primaryName, votes = primaryCandidates.pop()
                if self.hasNominationsFromAll(instId) or (
                        self.scheduledPrimaryDecisions[instId] is not None and
                        self.hasPrimaryDecisionTimerExpired(instId)):
                    logger.debug(
                        "{} has nominations from all so sending primary".format(
                            replica))
                    self.sendPrimary(instId, primaryName)
                else:
                    votesNeeded = math.ceil((self.nodeCount + 1) / 2.0)
                    if votes >= votesNeeded or (
                        self.scheduledPrimaryDecisions[instId] is not None and
                        self.hasPrimaryDecisionTimerExpired(instId)):
                        logger.debug(
                            "{} does not have nominations from all but "
                            "has {} votes for {} so sending primary"
                            .format(replica, votes, primaryName))
                        self.sendPrimary(instId, primaryName)
                        return
                    else:
                        logger.debug(
                            "{} has {} nominations for {}, but needs {}".
                            format(replica, votes, primaryName, votesNeeded))
                        self.schedulePrimaryDecision(instId)
                        return
            else:
                logger.debug("{} has {} nominations. Attempting reelection".
                             format(replica, self.nominations[instId]))
                if self.hasNominationsFromAll(instId) or (
                        self.scheduledPrimaryDecisions[instId] is not None and
                        self.hasPrimaryDecisionTimerExpired(instId)):
                    logger.info("{} proposing re-election".format(replica),
                                extra={"cli": True, "tags": ['node-election']})
                    self.sendReelection(instId,
                                        [n[0] for n in primaryCandidates])
                else:
                    # Does not have enough nominations for a re-election so wait
                    # for some time to get nominations from remaining nodes
                    logger.debug("{} waiting for more nominations".
                                 format(replica))
                    self.schedulePrimaryDecision(instId)

        else:
            logger.debug("{} has not got nomination quorum yet".format(replica))

    def sendNomination(self, name: str, instId: int, viewNo: int):
        """
        Broadcast a nomination message with the given parameters.

        :param name: node name
        :param instId: instance id
        :param viewNo: view number
        """
        self.send(Nomination(name, instId, viewNo))

    def sendPrimary(self, instId: int, primaryName: str):
        """
        Declare a primary and broadcast the message.

        :param instId: the instanceId to which the primary belongs
        :param primaryName: the name of the primary replica
        """
        replica = self.replicas[instId]
        self.primaryDeclarations[instId][replica.name] = primaryName
        self.scheduledPrimaryDecisions[instId] = None
        logger.debug("{} declaring primary as: {} on the basis of {}".
                     format(replica, primaryName,
                            self.nominations[instId]))
        self.send(Primary(primaryName, instId, self.viewNo))

    def sendReelection(self, instId: int,
                       primaryCandidates: Sequence[str] = None) -> None:
        """
        Broadcast a Reelection message.

        :param primaryCandidates: the candidates for primary election of the
        election round for which reelection is being conducted
        """
        replica = self.replicas[instId]
        self.reElectionRounds[instId] += 1
        primaryCandidates = primaryCandidates if primaryCandidates \
            else self.getPrimaryCandidates(instId)
        self.reElectionProposals[instId][replica.name] = primaryCandidates
        self.scheduledPrimaryDecisions[instId] = None
        logger.debug("{} declaring reelection round {} for: {}".
                     format(replica.name,
                            self.reElectionRounds[instId], primaryCandidates))
        self.send(
            Reelection(instId, self.reElectionRounds[instId], primaryCandidates,
                       self.viewNo))

    def getPrimaryCandidates(self, instId: int):
        """
        Return the list of primary candidates, i.e. the candidates with the
        maximum number of votes
        """
        candidates = Counter(self.nominations[instId].values()).most_common()
        # Candidates with max no. of votes
        return [c for c in candidates if c[1] == candidates[0][1]]

    def schedulePrimaryDecision(self, instId: int):
        """
        Schedule a primary decision for the protocol instance specified by
        `instId` if not already done.
        """
        replica = self.replicas[instId]
        if not self.scheduledPrimaryDecisions[instId]:
            logger.debug("{} scheduling primary decision".format(replica))
            self.scheduledPrimaryDecisions[instId] = time.perf_counter()
            self._schedule(partial(self.decidePrimary, instId),
                           (1 * self.nodeCount))
        else:
            logger.debug(
                "{} already scheduled primary decision".format(replica))
            if self.hasPrimaryDecisionTimerExpired(instId):
                logger.debug(
                    "{} executing already scheduled primary decision "
                    "since timer expired"
                    .format(replica))
                self._schedule(partial(self.decidePrimary, instId))

    def hasPrimaryDecisionTimerExpired(self, instId: int) -> bool:
        """
        Check whether there has been a timeout while waiting for elections.

        :param instId: id of the instance for which elections are happening.
        """
        return (time.perf_counter() - self.scheduledPrimaryDecisions[instId]) \
               > (1 * self.nodeCount)

    def send(self, msg):
        """
        Send a message to the node on which this replica resides.

        :param msg: the message to send
        """
        logger.debug("{}'s elector sending {}".format(self.name, msg))
        self.outBox.append(msg)

    def viewChanged(self, viewNo: int):
        """
        Actions to perform when a view change occurs.

        - Remove all pending messages which came for earlier views
        - Prepare for fresh elections
        - Schedule execution of any pending messages from the new view
        - Start elections again by nominating a random replica on this node

        :param viewNo: the new view number.
        """
        if viewNo > self.viewNo:
            self.viewNo = viewNo

            for replica in self.replicas:
                replica.primaryName = None

            # Remove all pending messages which came for earlier views
            oldViews = []
            for v in self.pendingMsgsForViews:
                if v < viewNo:
                    oldViews.append(v)

            for v in oldViews:
                self.pendingMsgsForViews.pop(v)

            # Reset to defaults values for different data structures as new
            # elections would begin
            for r in self.replicas:
                self.setDefaults(r.instId)
            self.replicaNominatedForItself = None

            # Schedule execution of any pending msgs from the new view
            if viewNo in self.pendingMsgsForViews:
                logger.debug("Pending election messages found for view {}".
                             format(viewNo))
                pendingMsgs = self.pendingMsgsForViews.pop(viewNo)
                self.inBox.extendleft(pendingMsgs)
            else:
                logger.debug(
                    "{} found no pending election messages for view {}".
                    format(self.name, viewNo))

            self.nominateRandomReplica()
        else:
            logger.warning("Provided view no {} is not greater than the "
                           "current view no {}".format(viewNo, self.viewNo))

    def getElectionMsgsForInstance(self, instId: int) -> \
            Sequence[Union[Nomination, Primary]]:
        """
        Get nomination and primary messages for instance with id `instId`.
        """
        msgs = []
        replica = self.replicas[instId]
        # If a primary for this instance has been selected then send a
        # primary declaration for the selected primary
        if replica.isPrimary is not None:
            msgs.append(Primary(replica.primaryName, instId, self.viewNo))
        else:
            # If a primary for this instance has not been selected then send
            # nomination and primary declaration that this node made for the
            # instance with id `instId`
            if self.didReplicaNominate(instId):
                msgs.append(Nomination(self.nominations[instId][
                                           replica.name],
                                       instId, self.viewNo))
            if self.didReplicaDeclarePrimary(instId):
                msgs.append(Primary(self.primaryDeclarations[instId][replica.name],
                                    instId,
                                    self.viewNo))
        return msgs

    def getElectionMsgsForLaggedNodes(self) -> \
            List[Union[Nomination, Primary]]:
        """
        Get nomination and primary messages for instance with id `instId` that
        need to be sent to a node which has lagged behind (for example, a newly
        started node, a node that has crashed and recovered etc.)
        """
        msgs = []
        for instId in range(len(self.replicas)):
            msgs.extend(self.getElectionMsgsForInstance(instId))
        return msgs

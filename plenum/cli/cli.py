from __future__ import unicode_literals

# noinspection PyUnresolvedReferences
import glob
from typing import Dict, Iterable

import pyorient
import shutil
from hashlib import sha256
from jsonpickle import json, encode, decode
from ledger.compact_merkle_tree import CompactMerkleTree
from ledger.ledger import Ledger
from ledger.stores.file_hash_store import FileHashStore
from os.path import basename, dirname

from plenum.cli.command import helpCmd, statusNodeCmd, statusClientCmd, \
    keyShareCmd, loadPluginsCmd, clientSendCmd, clientShowCmd, newKeyCmd, \
    newKeyringCmd, renameKeyringCmd, useKeyringCmd, saveKeyringCmd, \
    listKeyringCmd, listIdsCmd, useIdCmd, addGenesisTxnCmd, \
    createGenesisTxnFileCmd, changePromptCmd, exitCmd, quitCmd, Command
from plenum.cli.command import licenseCmd
from plenum.cli.command import newClientCmd
from plenum.cli.command import newNodeCmd
from plenum.cli.command import statusCmd
from plenum.cli.constants import SIMPLE_CMDS, CLI_CMDS, NODE_OR_CLI, NODE_CMDS, \
    PROMPT_ENV_SEPARATOR, WALLET_FILE_EXTENSION, NO_ENV
from plenum.cli.helper import getUtilGrams, getNodeGrams, getClientGrams, \
    getAllGrams
from plenum.cli.phrase_word_completer import PhraseWordCompleter
from plenum.client.wallet import Wallet
from plenum.common.exceptions import NameAlreadyExists, GraphStorageNotAvailable, \
    RaetKeysNotFoundException
from plenum.common.plugin_helper import loadPlugins
from plenum.common.port_dispenser import genHa
from plenum.common.raet import getLocalEstateData
from plenum.common.raet import isLocalKeepSetup
from plenum.common.signer_simple import SimpleSigner
from plenum.common.stack_manager import TxnStackManager
from plenum.common.txn import TXN_TYPE, TARGET_NYM, TXN_ID, DATA, IDENTIFIER, \
    NODE, ALIAS, NODE_IP, NODE_PORT, CLIENT_PORT, CLIENT_IP, VERKEY, BY
from prompt_toolkit.utils import is_windows, is_conemu_ansi

if is_windows():
    from prompt_toolkit.terminal.win32_output import Win32Output
    from prompt_toolkit.terminal.conemu_output import ConEmuOutput
else:
    from prompt_toolkit.terminal.vt100_output import Vt100_Output

import configparser
import os
from configparser import ConfigParser
from collections import OrderedDict
import time
import ast

from functools import reduce, partial
import logging
import sys
from collections import defaultdict

from prompt_toolkit.history import FileHistory
from ioflo.aid.consoling import Console
from prompt_toolkit.contrib.completers import WordCompleter
from prompt_toolkit.contrib.regular_languages.compiler import compile
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.interface import CommandLineInterface
from prompt_toolkit.shortcuts import create_prompt_application, \
    create_asyncio_eventloop
from prompt_toolkit.layout.lexers import SimpleLexer
from prompt_toolkit.styles import PygmentsStyle
from prompt_toolkit.terminal.vt100_output import Vt100_Output
from pygments.token import Token
from plenum.client.client import Client
from plenum.common.util import getMaxFailures, \
    firstValue, randomString, cleanSeed, bootstrapClientKeys, \
    createDirIfNotExists, getFriendlyIdentifier
from plenum.common.log import CliHandler, getlogger, Logger, \
    getRAETLogLevelFromConfig, getRAETLogFilePath, TRACE_LOG_LEVEL
from plenum.server.node import Node
from plenum.common.types import CLIENT_STACK_SUFFIX, NodeDetail, HA
from plenum.server.plugin_loader import PluginLoader
from plenum.server.replica import Replica
from plenum.common.config_util import getConfig
from plenum.__metadata__ import __version__


class CustomOutput(Vt100_Output):
    """
    Subclassing Vt100 just to override the `ask_for_cpr` method which prints
    an escape character on the console. Not printing the escape character
    """

    def ask_for_cpr(self):
        """
        Asks for a cursor position report (CPR).
        """
        self.flush()


class Cli:
    isElectionStarted = False
    primariesSelected = 0
    electedPrimaries = set()
    name = 'plenum'
    properName = 'Plenum'
    fullName = 'Plenum protocol'
    githubUrl = 'https://github.com/evernym/plenum'

    NodeClass = Node
    ClientClass = Client
    defaultWalletName = 'Default'

    _genesisTransactions = []

    # noinspection PyPep8
    def __init__(self, looper, basedirpath, nodeReg=None, cliNodeReg=None,
                 output=None, debug=False, logFileName=None, config=None,
                 useNodeReg=False, withNode=True, unique_name=None,
                 override_tags=None):
        self.unique_name = unique_name
        self.curClientPort = None
        self.basedirpath = os.path.expanduser(basedirpath)
        self._config = config or getConfig(self.basedirpath)

        Logger().enableCliLogging(self.out,
                                  override_tags=override_tags)
        self.looper = looper
        self.nodeRegLoadedFromFile = False
        if not (useNodeReg and nodeReg and len(nodeReg) and cliNodeReg
                and len(cliNodeReg)):
            self.nodeRegLoadedFromFile = True
            dataDir = self.basedirpath
            ledger = Ledger(CompactMerkleTree(hashStore=FileHashStore(
                dataDir=dataDir)),
                dataDir=dataDir,
                fileName=self.config.poolTransactionsFile)
            nodeReg, cliNodeReg, _ = TxnStackManager.parseLedgerForHaAndKeys(
                ledger)

        self.withNode = withNode
        self.nodeReg = nodeReg
        self.cliNodeReg = cliNodeReg
        self.nodeRegistry = {}
        for nStkNm, nha in self.nodeReg.items():
            cStkNm = nStkNm + CLIENT_STACK_SUFFIX
            self.nodeRegistry[nStkNm] = NodeDetail(HA(*nha), cStkNm,
                                                   HA(*self.cliNodeReg[cStkNm]))
        # Used to store created clients
        self.clients = {}  # clientName -> Client
        # To store the created requests
        self.requests = {}
        # To store the nodes created
        self.nodes = {}
        self.externalClientKeys = {}  # type: Dict[str,str]

        self.cliCmds = CLI_CMDS
        self.nodeCmds = NODE_CMDS
        self.helpablesCommands = self.cliCmds | self.nodeCmds
        self.simpleCmds = SIMPLE_CMDS
        self.commands = {'list', 'help'} | self.simpleCmds
        self.cliActions = {'send', 'show'}
        self.commands.update(self.cliCmds)
        self.commands.update(self.nodeCmds)
        self.node_or_cli = NODE_OR_CLI
        self.nodeNames = list(self.nodeReg.keys()) + ["all"]
        self.debug = debug
        self.plugins = {}
        self.pluginPaths = []
        self.defaultClient = None
        self.activeIdentifier = None
        # Wallet and Client are the same from user perspective for now
        self._activeClient = None
        self._wallets = {}  # type: Dict[str, Wallet]
        self._activeWallet = None  # type: Wallet
        self.keyPairs = {}
        '''
        examples:
        status

        new node Alpha
        new node all
        new client Joe
        client Joe send <Cmd>
        client Joe show 1
        '''

        self.utilGrams = getUtilGrams()

        self.nodeGrams = getNodeGrams()

        self.clientGrams = getClientGrams()

        self._allGrams = []

        self._lexers = {}

        self.clientWC = WordCompleter([])

        self._completers = {}

        self.initializeInputParser()

        self.style = PygmentsStyle.from_defaults({
            Token.Operator: '#33aa33 bold',
            Token.Gray: '#424242',
            Token.Number: '#aa3333 bold',
            Token.Name: '#ffff00 bold',
            Token.Heading: 'bold',
            Token.TrailingInput: 'bg:#662222 #ffffff',
            Token.BoldGreen: '#33aa33 bold',
            Token.BoldOrange: '#ff4f2f bold',
            Token.BoldBlue: '#095cab bold'})

        self.voidMsg = "<none>"

        # Create an asyncio `EventLoop` object. This is a wrapper around the
        # asyncio loop that can be passed into prompt_toolkit.
        eventloop = create_asyncio_eventloop(looper.loop)

        self.pers_hist = FileHistory('.{}-cli-history'.format(self.name))

        # Create interface.
        app = create_prompt_application('{}> '.format(self.name),
                                        lexer=self.grammarLexer,
                                        completer=self.grammarCompleter,
                                        style=self.style,
                                        history=self.pers_hist)
        self.currPromptText = self.name

        if output:
            out = output
        else:
            if is_windows():
                if is_conemu_ansi():
                    out = ConEmuOutput(sys.__stdout__)
                else:
                    out = Win32Output(sys.__stdout__)
            else:
                out = CustomOutput.from_pty(sys.__stdout__, true_color=True)

        self.cli = CommandLineInterface(
            application=app,
            eventloop=eventloop,
            output=out)

        RAETVerbosity = getRAETLogLevelFromConfig("RAETLogLevelCli",
                                                  Console.Wordage.mute,
                                                  self.config)
        RAETLogFile = getRAETLogFilePath("RAETLogFilePathCli", self.config)
        # Patch stdout in something that will always print *above* the prompt
        # when something is written to stdout.
        sys.stdout = self.cli.stdout_proxy()

        if logFileName:
            Logger().enableFileLogging(logFileName)
        Logger().setupRaet(RAETVerbosity, RAETLogFile)

        self.logger = getlogger("cli")
        self.print("\n{}-CLI (c) 2017 Evernym, Inc.".format(self.properName))
        self._actions = []

        if nodeReg:
            self.print("Node registry loaded.")
            self.showNodeRegistry()

        self.print("Type 'help' for more information.")
        self.print("Running {} {}\n".format(self.properName,
                                            self.getCliVersion()))

        tp = loadPlugins(self.basedirpath)
        self.logger.debug("total plugins loaded in cli: {}".format(tp))

        self.restoreLastActiveWallet()

        self.checkIfCmdHandlerAndCmdMappingExists()

    def _getCmdMappingError(self, cmdHandlerFuncName, mappingFuncName):
        msg="Command mapping not provided for '{}' command handler. " \
            "\nPlease add proper mapping for that command handler " \
            "(in function '{}') with corresponding command object.".\
            format(cmdHandlerFuncName, mappingFuncName)

        sep = "\n" + "*"*125 + "\n"
        msg = sep + msg + sep
        return msg

    def checkIfCmdHandlerAndCmdMappingExists(self):
        for cmdHandlerFunc in self.actions:
            funcName = cmdHandlerFunc.__name__.replace("_","")
            if funcName not in self.cmdHandlerToCmdMappings().keys():
                raise Exception(self._getCmdMappingError(
                    cmdHandlerFunc.__name__,
                    self.cmdHandlerToCmdMappings.__name__))

    @staticmethod
    def getCliVersion():
        return __version__

    @property
    def genesisTransactions(self):
        return self._genesisTransactions

    def reset(self):
        self._genesisTransactions = []

    @property
    def actions(self):
        if not self._actions:
            self._actions = [self._simpleAction, self._helpAction,
                             self._newNodeAction, self._newClientAction,
                             self._statusNodeAction, self._statusClientAction,
                             self._keyShareAction, self._loadPluginDirAction,
                             self._clientCommand, self._addKeyAction,
                             self._newKeyAction, self._listIdsAction,
                             self._useIdentifierAction, self._addGenesisAction,
                             self._createGenTxnFileAction, self._changePrompt,
                             self._newKeyring, self._renameKeyring,
                             self._useKeyringAction, self._saveKeyringAction,
                             self._listKeyringsAction ]
        return self._actions

    @property
    def config(self):
        if self._config:
            return self._config
        else:
            self._config = getConfig()
            return self._config

    @property
    def allGrams(self):
        if not self._allGrams:
            self._allGrams = [self.utilGrams, self.nodeGrams, self.clientGrams]
        return self._allGrams

    @property
    def completers(self):
        if not self._completers:
            self._completers = {
                'node_command': WordCompleter(self.nodeCmds),
                'client_command': WordCompleter(self.cliCmds),
                'client': WordCompleter(['client']),
                'command': WordCompleter(self.commands),
                'node_or_cli': WordCompleter(self.node_or_cli),
                'node_name': WordCompleter(self.nodeNames),
                'more_nodes': WordCompleter(self.nodeNames),
                'helpable': WordCompleter(self.helpablesCommands),
                'load_plugins': PhraseWordCompleter('load plugins from'),
                'client_name': self.clientWC,
                'second_client_name': self.clientWC,
                'cli_action': WordCompleter(self.cliActions),
                'simple': WordCompleter(self.simpleCmds),
                'add_key': PhraseWordCompleter('add key'),
                'for_client': PhraseWordCompleter('for client'),
                'new_key': PhraseWordCompleter('new key'),
                'new_keyring': PhraseWordCompleter('new keyring'),
                'rename_keyring': PhraseWordCompleter('rename keyring'),
                'list_ids': PhraseWordCompleter('list ids'),
                'list_krs': PhraseWordCompleter('list keyrings'),
                'become': WordCompleter(['become']),
                'use_id': PhraseWordCompleter('use identifier'),
                'use_kr': PhraseWordCompleter('use keyring'),
                'save_kr': PhraseWordCompleter('save keyring'),
                'add_gen_txn': PhraseWordCompleter('add genesis transaction'),
                'prompt': WordCompleter(['prompt']),
                'create_gen_txn_file': PhraseWordCompleter(
                    'create genesis transaction file')
            }
        return self._completers

    @property
    def lexers(self):
        if not self._lexers:
            lexerNames = {
                'node_command',
                'command',
                'helpable',
                'load_plugins',
                'load',
                'node_or_cli',
                'node_name',
                'more_nodes',
                'simple',
                'client_command',
                'add_key',
                'verkey',
                'for_client',
                'identifier',
                'new_key',
                'list_ids',
                'list_krs',
                'become',
                'use_id',
                'prompt',
                'new_keyring',
                'use_kr',
                'save_kr',
                'rename_keyring',
                'add_genesis',
                'create_gen_txn_file'
            }
            lexers = {n: SimpleLexer(Token.Keyword) for n in lexerNames}
            self._lexers = {**lexers}
        return self._lexers

    def _renameWalletFile(self, oldWalletName, newWalletName):
        keyringsDir = self.getContextBasedKeyringsBaseDir()
        oldWalletFilePath = Cli.getWalletFilePath(
            keyringsDir, Cli._normalizedWalletFileName(oldWalletName))
        if os.path.exists(oldWalletFilePath):
            newWalletFilePath = Cli.getWalletFilePath(
            keyringsDir, Cli._normalizedWalletFileName(newWalletName))
            if os.path.exists(newWalletFilePath):
                self.print("A persistent wallet file already exists for "
                           "new wallet name. Please choose new wallet name.")
                return False
            os.rename(oldWalletFilePath, newWalletFilePath)
        return True

    def _renameKeyring(self, matchedVars):
        if matchedVars.get('rename_keyring'):
            fromName = matchedVars.get('from')
            toName = matchedVars.get('to')
            conflictFound = self._checkIfIdentifierConflicts(
                toName, checkInAliases=False, checkInSigners=False)
            if not conflictFound:
                fromWallet = self.wallets.get(fromName) if fromName \
                    else self.activeWallet
                if not fromWallet:
                    self.print('Keyring {} not found'.format(fromName))
                    return True

                if not self._renameWalletFile(fromName, toName):
                    return True

                fromWallet.name = toName
                del self.wallets[fromName]
                self.wallets[toName] = fromWallet

                self.print('Keyring {} renamed to {}'.format(fromName, toName))
            return True

    def _newKeyring(self, matchedVars):
        if matchedVars.get('new_keyring'):
            name = matchedVars.get('name')
            conflictFound = self._checkIfIdentifierConflicts(
                name, checkInAliases=False, checkInSigners=False)
            if not conflictFound:
                self._saveActiveWallet()
                self._newWallet(name)
            return True

    def _changePrompt(self, matchedVars):
        if matchedVars.get('prompt'):
            promptText = matchedVars.get('name')
            self._setPrompt(promptText)
            return True

    def _createGenTxnFileAction(self, matchedVars):
        if matchedVars.get('create_gen_txn_file'):
            ledger = Ledger(CompactMerkleTree(),
                            dataDir=self.basedirpath,
                            fileName=self.config.poolTransactionsFile)
            ledger.reset()
            for item in self.genesisTransactions:
                ledger.add(item)
            self.print('Genesis transaction file created at {} '
                       .format(ledger._transactionLog.dbPath))
            return True

    def _addGenesisAction(self, matchedVars):
        if matchedVars.get('add_gen_txn'):
            if matchedVars.get(TARGET_NYM):
                return self._addOldGenesisCommand(matchedVars)
            else:
                return self._addNewGenesisCommand(matchedVars)

    def _addNewGenesisCommand(self, matchedVars):
        typ = matchedVars.get(TXN_TYPE)

        nodeName, nodeData, identifier = None, None, None
        jsonNodeData = json.loads(matchedVars.get(DATA))
        for key, value in jsonNodeData.items():
            if key == BY:
                identifier = value
            else:
                nodeName, nodeData = key, value

        withData = {ALIAS: nodeName}

        if typ == NODE:
            nodeIp, nodePort = nodeData.get('node_address').split(':')
            clientIp, clientPort = nodeData.get('client_address').split(':')
            withData[NODE_IP] = nodeIp
            withData[NODE_PORT] = int(nodePort)
            withData[CLIENT_IP] = clientIp
            withData[CLIENT_PORT] = int(clientPort)

        newMatchedVars = {TXN_TYPE: typ, DATA: json.dumps(withData),
                          TARGET_NYM: nodeData.get(VERKEY),
                          IDENTIFIER: identifier}
        return self._addOldGenesisCommand(newMatchedVars)

    def _addOldGenesisCommand(self, matchedVars):
        destId = getFriendlyIdentifier(matchedVars.get(TARGET_NYM))
        typ = matchedVars.get(TXN_TYPE)
        txn = {
            TXN_TYPE: typ,
            TARGET_NYM: destId,
            TXN_ID: sha256(randomString(6).encode()).hexdigest(),
        }
        if matchedVars.get(IDENTIFIER):
            txn[IDENTIFIER] = getFriendlyIdentifier(matchedVars.get(IDENTIFIER))

        if matchedVars.get(DATA):
            txn[DATA] = json.loads(matchedVars.get(DATA))

        self.genesisTransactions.append(txn)
        self.print('Genesis transaction added')
        return True

    def _buildClientIfNotExists(self, config=None):
        if not self._activeClient:
            if not self.activeWallet:
                print("Keyring is not initialized")
                return
            # Need a unique name so nodes can differentiate
            name = self.name + randomString(6)
            self.newClient(clientName=name, config=config)

    @property
    def wallets(self):
        return self._wallets

    @property
    def activeWallet(self) -> Wallet:
        if not self._activeWallet:
            if self.wallets:
                self.activeWallet = firstValue(self.wallets)
            else:
                self.activeWallet = self._newWallet()
        return self._activeWallet

    @activeWallet.setter
    def activeWallet(self, wallet):
        self._activeWallet = wallet
        self.print('Active keyring set to "{}"'.format(wallet.name))

    @property
    def activeClient(self):
        self._buildClientIfNotExists()
        return self._activeClient

    @activeClient.setter
    def activeClient(self, client):
        self._activeClient = client
        self.print("Active client set to " + client.name)

    @staticmethod
    def relist(seq):
        return '(' + '|'.join(seq) + ')'

    def initializeInputParser(self):
        self.initializeGrammar()
        self.initializeGrammarLexer()
        self.initializeGrammarCompleter()

    def initializeGrammar(self):
        # TODO Do we really need both self.allGrams and self.grams
        self.grams = getAllGrams(*self.allGrams)
        self.grammar = compile("".join(self.grams))

    def initializeGrammarLexer(self):
        self.grammarLexer = GrammarLexer(self.grammar, lexers=self.lexers)

    def initializeGrammarCompleter(self):
        self.grammarCompleter = GrammarCompleter(self.grammar, self.completers)

    def print(self, msg, token=None, newline=True):
        if newline:
            msg += "\n"
        part = partial(self.cli.print_tokens, [(token, msg)])
        if self.debug:
            part()
        else:
            self.cli.run_in_terminal(part)

    def printVoid(self):
        self.print(self.voidMsg)

    def out(self, record, extra_cli_value=None):
        """
        Callback so that this cli can manage colors

        :param record: a log record served up from a custom handler
        :param extra_cli_value: the "cli" value in the extra dictionary
        :return:
        """
        if extra_cli_value in ("IMPORTANT", "ANNOUNCE"):
            self.print(record.msg, Token.BoldGreen)  # green
        elif extra_cli_value in ("WARNING",):
            self.print(record.msg, Token.BoldOrange)  # orange
        elif extra_cli_value in ("STATUS",):
            self.print(record.msg, Token.BoldBlue)  # blue
        elif extra_cli_value in ("PLAIN", "LOW_STATUS"):
            self.print(record.msg, Token)  # white
        else:
            self.print(record.msg, Token)

    def cmdHandlerToCmdMappings(self):
        # The 'key' of 'mappings' dictionary is action handler function name
        # without leading underscore sign. Each such funcation name should be
        # mapped here, its other thing that if you don't want to display it
        # in help, map it to None, but mapping should be present, that way it
        # will force developer to either write help message for those cli
        # commands or make a decision to not show it in help message.

        mappings = OrderedDict()
        mappings['helpAction'] = helpCmd
        mappings['statusAction'] = statusCmd
        mappings['changePrompt'] = changePromptCmd
        mappings['loadPluginDirAction'] = loadPluginsCmd

        mappings['newKeyring'] = newKeyringCmd
        mappings['renameKeyring'] = renameKeyringCmd
        mappings['useKeyringAction'] = useKeyringCmd
        mappings['saveKeyringAction'] = saveKeyringCmd
        mappings['listKeyringsAction'] = listKeyringCmd

        mappings['newKeyAction'] = newKeyCmd
        mappings['useIdentifierAction'] = useIdCmd
        mappings['listIdsAction'] = listIdsCmd

        mappings['newNodeAction'] = newNodeCmd
        mappings['newClientAction'] = newClientCmd
        mappings['statusNodeAction'] = statusNodeCmd
        mappings['statusClientAction'] = statusClientCmd
        mappings['keyShareAction'] = keyShareCmd
        mappings['clientSendMsgCommand'] = clientSendCmd
        mappings['clientShowMsgCommand'] = clientShowCmd

        mappings['addGenesisAction'] = addGenesisTxnCmd
        mappings['createGenTxnFileAction'] = createGenesisTxnFileCmd
        mappings['licenseAction'] = licenseCmd
        mappings['quitAction'] = quitCmd
        mappings['exitAction'] = exitCmd

        # below action handlers are those who handles multiple commands and so
        # these will point to 'None' and specific commands will point to their
        # corresponding help msgs.
        mappings['clientCommand'] = None
        mappings['simpleAction'] = None

        # TODO: These seems to be obsolete, so either we need to remove these
        # command handlers or let it point to None
        mappings['addKeyAction'] = None         # obsolete command


        return mappings

    def getTopComdMappingKeysForHelp(self):
        return ['helpAction', 'statusAction']

    def getComdMappingKeysToNotShowInHelp(self):
        return ['quitAction']

    def getBottomComdMappingKeysForHelp(self):
        return ['licenseAction', 'exitAction']

    def getDefaultOrderedCmds(self):
        topCmdKeys = self.getTopComdMappingKeysForHelp()
        removeCmdKeys = self.getComdMappingKeysToNotShowInHelp()
        bottomCmdsKeys = self.getBottomComdMappingKeysForHelp()

        topCmds = [self.cmdHandlerToCmdMappings().get(k) for k in topCmdKeys]
        bottomCmds = [self.cmdHandlerToCmdMappings().get(k) for k in bottomCmdsKeys]
        middleCmds = [v for k, v in self.cmdHandlerToCmdMappings().items()
                      if k not in topCmdKeys
                      and k not in bottomCmdsKeys
                      and k not in removeCmdKeys]
        return [c for c in (topCmds + middleCmds + bottomCmds) if c is not None]

    def _printGivenCmdsHelpMsgs(self, cmds: Iterable[Command], gapsInLines=1,
                                sort=False, printHeader=True, showUsageFor=[]):
        helpMsgStr = ""
        if printHeader:
            helpMsgStr += "{}-CLI, a simple command-line interface for a {}.".\
                format(self.properName, self.fullName)

        helpMsgStr += "\n   Commands:"

        if sort:
            cmds = sorted(cmds, key=lambda hm: hm.id)

        for cmd in cmds:
            helpMsgLines = cmd.title.split("\n")
            helpMsgFormattedLine = "\n         ".join(helpMsgLines)

            helpMsgStr += "{}       {} - {}".format(
                '\n'*gapsInLines, cmd.id, helpMsgFormattedLine)

            if cmd.id in showUsageFor:
                helpMsgStr += "\n         Usage:\n            {}".\
                    format(cmd.usage)

        self.print("\n{}\n".format(helpMsgStr))

    def getHelpCmdIdsToShowUsage(self):
        return ["help"]

    def printHelp(self):
        self._printGivenCmdsHelpMsgs(self.getDefaultOrderedCmds(),
                                     sort=False, printHeader=True,
                                     showUsageFor=self.getHelpCmdIdsToShowUsage())

    @staticmethod
    def joinTokens(tokens, separator=None, begin=None, end=None):
        if separator is None:
            separator = (Token, ', ')
        elif isinstance(separator, str):
            separator = (Token, separator)
        r = reduce(lambda x, y: x + [separator, y] if x else [y], tokens, [])
        if begin is not None:
            b = (Token, begin) if isinstance(begin, str) else begin
            r = [b] + r
        if end:
            if isinstance(end, str):
                r.append((Token, end))
        return r

    def printTokens(self, tokens, separator=None, begin=None, end=None):
        x = self.joinTokens(tokens, separator, begin, end)
        self.cli.print_tokens(x, style=self.style)

    def printNames(self, names, newline=False):
        tokens = [(Token.Name, n) for n in names]
        self.printTokens(tokens)
        if newline:
            self.printTokens([(Token, "\n")])

    def showValidNodes(self):
        self.printTokens([(Token, "Valid node names are: ")])
        self.printNames(self.nodeReg.keys(), newline=True)

    def showNodeRegistry(self):
        t = []
        for name in self.nodeReg:
            ip, port = self.nodeReg[name]
            t.append((Token.Name, "    " + name))
            t.append((Token, ": {}:{}\n".format(ip, port)))
        self.cli.print_tokens(t, style=self.style)

    def loadFromFile(self, file: str) -> None:
        cfg = ConfigParser()
        cfg.read(file)
        self.nodeReg = Cli.loadNodeReg(cfg)
        self.cliNodeReg = Cli.loadCliNodeReg(cfg)

    @classmethod
    def loadNodeReg(cls, cfg: ConfigParser) -> OrderedDict:
        return cls._loadRegistry(cfg, 'node_reg')

    @classmethod
    def loadCliNodeReg(cls, cfg: ConfigParser) -> OrderedDict:
        try:
            return cls._loadRegistry(cfg, 'client_node_reg')
        except configparser.NoSectionError:
            return OrderedDict()

    @classmethod
    def _loadRegistry(cls, cfg: ConfigParser, reg: str):
        registry = OrderedDict()
        for n in cfg.items(reg):
            host, port = n[1].split()
            registry.update({n[0]: (host, int(port))})
        return registry

    def getStatus(self):
        self.print('Nodes: ', newline=False)
        if not self.nodes:
            self.print("No nodes are running. Try typing 'new node <name>'.")
        else:
            self.printNames(self.nodes, newline=True)
        if not self.clients:
            clients = "No clients are running. Try typing 'new client <name>'."
        else:
            clients = ",".join(self.clients.keys())
        self.print("Clients: " + clients)
        f = getMaxFailures(len(self.nodes))
        self.print("f-value (number of possible faulty nodes): {}".format(f))
        if f != 0 and len(self.nodes) >= 2 * f + 1:
            node = list(self.nodes.values())[0]
            mPrimary = node.replicas[node.instances.masterId].primaryName
            bPrimary = node.replicas[node.instances.backupIds[0]].primaryName
            self.print("Instances: {}".format(f + 1))
            if mPrimary:
                self.print("   Master (primary is on {})".
                           format(Replica.getNodeName(mPrimary)))
            if bPrimary:
                self.print("   Backup (primary is on {})".
                           format(Replica.getNodeName(bPrimary)))
        else:
            self.print("Instances: "
                       "Not enough nodes to create protocol instances")

    def keyshare(self, nodeName):
        node = self.nodes.get(nodeName, None)
        if node is not None:
            node = self.nodes[nodeName]
            node.startKeySharing()
        elif nodeName not in self.nodeReg:
            tokens = [(Token.Error, "Invalid node name '{}'.".format(nodeName))]
            self.printTokens(tokens)
            self.showValidNodes()
            return
        else:
            tokens = [(Token.Error, "Node '{}' not started.".format(nodeName))]
            self.printTokens(tokens)
            self.showStartedNodes()
            return

    def showStartedNodes(self):
        self.printTokens([(Token, "Started nodes are: ")])
        startedNodes = self.nodes.keys()
        if startedNodes:
            self.printNames(self.nodes.keys(), newline=True)
        else:
            self.print("None", newline=True)

    def isOkToRunNodeDependentCommands(self):
        if not self.withNode:
            self.print("This command is only available if you start "
                       "this cli with command line argument --with-node "
                       "(and it assumes you have installed sovrin-node "
                       "dependency)")
            return False
        if not self.NodeClass:
            self.print("This command requires sovrin-node dependency, "
                       "please install it and then resume.")
            return False

        return True

    def newNode(self, nodeName: str):
        if not self.isOkToRunNodeDependentCommands():
            return

        if len(self.clients) > 0 and not self.hasAnyKey:
            return

        if nodeName in self.nodes:
            self.print("Node {} already exists.".format(nodeName))
            return

        if nodeName == "all":
            names = set(self.nodeReg.keys()) - set(self.nodes.keys())
        elif nodeName not in self.nodeReg:
            tokens = [
                (Token.Error, "Invalid node name '{}'. ".format(nodeName))]
            self.printTokens(tokens)
            self.showValidNodes()
            return
        else:
            names = [nodeName]

        nodes = []
        for name in names:
            try:
                node = self.NodeClass(name,
                                  nodeRegistry=None if self.nodeRegLoadedFromFile
                                  else self.nodeRegistry,
                                  basedirpath=self.basedirpath,
                                  pluginPaths=self.pluginPaths,
                                  config=self.config)
            except (GraphStorageNotAvailable, RaetKeysNotFoundException) as e:
                self.print(str(e), Token.BoldOrange)
                return
            self.nodes[name] = node
            self.looper.add(node)
            if not self.nodeRegLoadedFromFile:
                node.startKeySharing()

            if len(self.clients) > 0:
                self.bootstrapKey(self.activeWallet, node)

            for identifier, verkey in self.externalClientKeys.items():
                node.clientAuthNr.addClient(identifier, verkey)
            nodes.append(node)
        return nodes

    def ensureValidClientId(self, clientName):
        """
        Ensures client id is not already used or is not starting with node
        names.

        :param clientName:
        :return:
        """
        if clientName in self.clients:
            raise ValueError("Client {} already exists.".format(clientName))

        if any([clientName.startswith(nm) for nm in self.nodeNames]):
            raise ValueError("Client name cannot start with node names, "
                             "which are {}."
                             .format(', '.join(self.nodeReg.keys())))

    def statusClient(self, clientName):
        if clientName == "all":
            for nm in self.clients:
                self.statusClient(nm)
            return
        if clientName not in self.clients:
            self.print("client not found", Token.Error)
        else:
            self.print("    Name: " + clientName)
            client = self.clients[clientName]  # type: Client

            self.printTokens([(Token.Heading, 'Status for client:'),
                              (Token.Name, client.name)],
                             separator=' ', end='\n')
            self.print("    age (seconds): {:.0f}".format(
                time.perf_counter() - client.created))
            self.print("    status: {}".format(client.status.name))
            self.print("    connected to: ", newline=False)
            if client.nodestack.conns:
                self.printNames(client.nodestack.conns, newline=True)
            else:
                self.printVoid()
            if self.activeWallet and self.activeWallet.defaultId:
                wallet = self.activeWallet
                idr = wallet.defaultId
                self.print("    Identifier: {}".format(idr))
                self.print(
                    "    Verification key: {}".format(wallet.getVerkey(idr)))

    def statusNode(self, nodeName):
        if nodeName == "all":
            for nm in self.nodes:
                self.statusNode(nm)
            return
        if nodeName not in self.nodes:
            self.print("Node {} not found".format(nodeName), Token.Error)
        else:
            self.print("\n    Name: " + nodeName)
            node = self.nodes[nodeName]  # type: Node
            ip, port = self.nodeReg.get(nodeName)
            nha = "0.0.0.0:{}".format(port)
            self.print("    Node listener: " + nha)
            ip, port = self.cliNodeReg.get(nodeName + CLIENT_STACK_SUFFIX)
            cha = "0.0.0.0:{}".format(port)
            self.print("    Client listener: " + cha)
            self.print("    Status: {}".format(node.status.name))
            self.print('    Connections: ', newline=False)
            connecteds = node.nodestack.connecteds
            if connecteds:
                self.printNames(connecteds, newline=True)
            else:
                self.printVoid()
            notConnecteds = list({r for r in self.nodes.keys()
                                  if r not in connecteds
                                  and r != nodeName})
            if notConnecteds:
                self.print('    Not connected: ', newline=False)
                self.printNames(notConnecteds, newline=True)
            self.print("    Replicas: {}".format(len(node.replicas)),
                       newline=False)
            if node.hasPrimary:
                if node.primaryReplicaNo == 0:
                    self.print("  (primary of Master)")
                else:
                    self.print("  (primary of Backup)")
            else:
                self.print("   (no primary replicas)")
            self.print("    Up time (seconds): {:.0f}".
                       format(time.perf_counter() - node.created))
            self.print("    Clients: ", newline=False)
            clients = node.clientstack.connecteds
            if clients:
                self.printNames(clients, newline=True)
            else:
                self.printVoid()

    def newClient(self, clientName,
                  config=None):
        try:
            self.ensureValidClientId(clientName)
            if not isLocalKeepSetup(clientName, self.basedirpath):
                client_addr = genHa(ip='0.0.0.0')
            else:
                client_addr = tuple(getLocalEstateData(clientName,
                                                       self.basedirpath)['ha'])
            nodeReg = None if self.nodeRegLoadedFromFile else self.cliNodeReg
            client = self.ClientClass(clientName,
                                      ha=client_addr,
                                      nodeReg=nodeReg,
                                      basedirpath=self.basedirpath,
                                      config=config)
            self.activeClient = client
            self.looper.add(client)
            self.clients[clientName] = client
            self.clientWC.words = list(self.clients.keys())
            return client
        except ValueError as ve:
            self.print(ve.args[0], Token.Error)

    @staticmethod
    def bootstrapKey(wallet, node, identifier=None):
        identifier = identifier or wallet.defaultId
        assert identifier, "Client has no identifier"
        node.clientAuthNr.addClient(identifier, wallet.getVerkey(identifier))

    def clientExists(self, clientName):
        return clientName in self.clients

    def printMsgForUnknownClient(self):
        self.print("No such client. See: 'help new client' for more details")

    def printMsgForUnknownWallet(self, walletName):
        self.print("No such wallet {}.".format(walletName))

    def sendMsg(self, clientName, msg):
        client = self.clients.get(clientName, None)
        wallet = self.wallets.get(clientName, None)  # type: Wallet
        if client:
            if wallet:
                req = wallet.signOp(msg)
                request, = client.submitReqs(req)
                self.requests[request.key] = request
                self.print("Request sent, request id: {}".format(req.reqId), Token.BoldBlue)
            else:
                try:
                    self._newWallet(clientName)
                    self.printNoKeyMsg()
                except NameAlreadyExists:
                    self.print("Keyring with name {} is not in use, please select it by using 'use keyring {}' command"
                               .format(clientName, clientName))

        else:
            self.printMsgForUnknownClient()

    def getReply(self, clientName, identifier, reqId):
        reqId = int(reqId)
        client = self.clients.get(clientName, None)
        if client and (identifier, reqId) in self.requests:
            reply, status = client.getReply(identifier, reqId)
            self.print("Reply for the request: {}".format(reply))
            self.print("Status: {}".format(status))
        elif not client:
            self.printMsgForUnknownClient()
        else:
            self.print("No such request. See: 'help client show request status' for more details")


    async def shell(self, *commands, interactive=True):
        """
        Coroutine that runs command, including those from an interactive
        command line.

        :param commands: an iterable of commands to run first
        :param interactive: when True, this coroutine will process commands
            entered on the command line.
        :return:
        """
        # First handle any commands passed in
        for command in commands:
            if not command.startswith("--"):
                self.print("\nRunning command: '{}'...\n".format(command))
                self.parse(command)

        # then handle commands from the prompt
        while interactive:
            try:
                result = await self.cli.run_async()
                cmd = result.text if result else ""
                cmds = cmd.strip().splitlines()
                for c in cmds:
                    self.parse(c)
            except Exit:
                break
            except (EOFError, KeyboardInterrupt):
                self._saveActiveWallet()
                break

        self.print('Goodbye.')

    def _simpleAction(self, matchedVars):
        if matchedVars.get('simple'):
            cmd = matchedVars.get('simple')
            if cmd == 'status':
                self.getStatus()
            elif cmd == 'license':
                self._showLicense()
            elif cmd in ['exit', 'quit']:
                self._saveActiveWallet()
                raise Exit
            return True

    def _showLicense(self):
        self.print("""
                                Copyright 2016 Evernym, Inc.
                Licensed under the Apache License, Version 2.0 (the "License");
                you may not use this file except in compliance with the License.
                You may obtain a copy of the License at

                    http://www.apache.org/licenses/LICENSE-2.0

                Unless required by applicable law or agreed to in writing, software
                distributed under the License is distributed on an "AS IS" BASIS,
                WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
                See the License for the specific language governing permissions and
                limitations under the License.
                    """)

    def getMatchedHelpableMsg(self, helpable):
        matchedHelpMsgs = [hm for hm in self.cmdHandlerToCmdMappings().values() if hm and hm.id == helpable]
        if matchedHelpMsgs:
            return matchedHelpMsgs[0]
        return None

    def _helpAction(self, matchedVars):
        if matchedVars.get('command') == 'help':
            helpable = matchedVars.get('helpable')
            if helpable:
                matchedHelpMsg = self.getMatchedHelpableMsg(helpable)
                if matchedHelpMsg:
                    self.print(str(matchedHelpMsg))
                else:
                    self.print("No such command found: {}\n".format(helpable))
                    self.printHelp()
            else:
                self.printHelp()
            return True

    def _newNodeAction(self, matchedVars):
        if matchedVars.get('node_command') == 'new':
            self.createEntities('node_name', 'more_nodes',
                                matchedVars, self.newNode)
            return True

    def _newClientAction(self, matchedVars):
        if matchedVars.get('client_command') == 'new':
            self.createEntities('client_name', 'more_clients',
                                matchedVars, self.newClient)
            return True

    def _statusNodeAction(self, matchedVars):
        if matchedVars.get('node_command') == 'status':
            node = matchedVars.get('node_name')
            self.statusNode(node)
            return True

    def _statusClientAction(self, matchedVars):
        if matchedVars.get('client_command') == 'status':
            client = matchedVars.get('client_name')
            self.statusClient(client)
            return True

    def _keyShareAction(self, matchedVars):
        if matchedVars.get('node_command') == 'keyshare':
            name = matchedVars.get('node_name')
            self.keyshare(name)
            return True

    def _clientCommand(self, matchedVars):
        if matchedVars.get('client') == 'client':
            client_name = matchedVars.get('client_name')
            client_action = matchedVars.get('cli_action')
            if client_action == 'send':
                msg = matchedVars.get('msg')
                try:
                    actualMsgRepr = ast.literal_eval(msg)
                except Exception as ex:
                    self.print("error evaluating msg expression: {}".
                               format(ex), Token.BoldOrange)
                    return True
                self.sendMsg(client_name, actualMsgRepr)
                return True
            elif client_action == 'show':
                req_id = matchedVars.get('req_id')
                self.getReply(client_name, self.activeWallet.defaultId, req_id)
                return True

    def _loadPluginDirAction(self, matchedVars):
        if matchedVars.get('load_plugins') == 'load plugins from':
            pluginsPath = matchedVars.get('plugin_dir')
            try:
                plugins = PluginLoader(
                    pluginsPath).plugins  # type: Dict[str, Set]
                for pluginSet in plugins.values():
                    for plugin in pluginSet:
                        if hasattr(plugin, "supportsCli") and plugin.supportsCli:
                            plugin.cli = self
                            parserReInitNeeded = False
                            if hasattr(plugin, "grams") and \
                                    isinstance(plugin.grams,
                                               list) and plugin.grams:
                                self._allGrams.append(plugin.grams)
                                parserReInitNeeded = True
                            # TODO Need to check if `plugin.cliActionNames`
                            #  conflicts with any of `self.cliActions`
                            if hasattr(plugin, "cliActionNames") and \
                                    isinstance(plugin.cliActionNames, set) and \
                                    plugin.cliActionNames:
                                self.cliActions.update(plugin.cliActionNames)
                                # TODO: Find better way to reinitialize completers
                                # , also might need to reinitialize lexers
                                self._completers = {}
                                parserReInitNeeded = True
                            if parserReInitNeeded:
                                self.initializeInputParser()
                                self.cli.application.buffer.completer = \
                                    self.grammarCompleter
                                self.cli.application.layout.children[
                                    1].children[
                                    1].content.content.lexer = self.grammarLexer
                            if hasattr(plugin, "actions") and \
                                    isinstance(plugin.actions, list):
                                self._actions.extend(plugin.actions)

                self.plugins.update(plugins)
                self.pluginPaths.append(pluginsPath)
            except FileNotFoundError as ex:
                _, err = ex.args
                self.print(err, Token.BoldOrange)
            return True

    def _addKeyAction(self, matchedVars):
        if matchedVars.get('add_key') == 'add key':
            verkey = matchedVars.get('verkey')
            # TODO make verkey case insensitive
            identifier = matchedVars.get('identifier')
            if identifier in self.externalClientKeys:
                self.print("identifier already added", Token.Error)
                return
            self.externalClientKeys[identifier] = verkey
            for n in self.nodes.values():
                n.clientAuthNr.addClient(identifier, verkey)
            return True

    def _addSignerToGivenWallet(self, signer, wallet: Wallet=None,
                                showMsg: bool=False):
        if not wallet:
            wallet = self._newWallet()
        wallet.addIdentifier(signer=signer)
        if showMsg:
            self.print("Key created in keyring " + wallet.name)

    def _newSigner(self,
                   wallet=None,
                   identifier=None,
                   seed=None,
                   alias=None):

        cseed = cleanSeed(seed)

        signer = SimpleSigner(identifier=identifier, seed=cseed, alias=alias)
        self._addSignerToGivenWallet(signer, wallet, showMsg=True)
        self.print("Identifier for key is {}".format(signer.identifier))
        if alias:
            self.print("Alias for identifier is {}".format(signer.alias))
        self._setActiveIdentifier(signer.identifier)
        self.bootstrapClientKeys(signer.identifier, signer.verkey,
                                 self.nodes.values())
        return signer

    @staticmethod
    def bootstrapClientKeys(idr, verkey, nodes):
        bootstrapClientKeys(idr, verkey, nodes)

    def isValidSeedForNewKey(self, seed):
        if seed:
            seed = seed.strip()
            if len(seed) != 32:
                self.print('Seed needs to be 32 characters long but is {} '
                           'characters long'.format(len(seed)), Token.Error)
                return False

        return True

    def _newKeyAction(self, matchedVars):
        if matchedVars.get('new_key') == 'new key':
            seed = matchedVars.get('seed')
            if not self.isValidSeedForNewKey(seed):
                return True
            alias = matchedVars.get('alias')
            if alias:
                alias = alias.strip()
            self._newSigner(seed=seed, alias=alias, wallet=self.activeWallet)
            return True

    def _buildWalletClass(self, nm):
        return self.walletClass(nm)

    @property
    def walletClass(self):
        return Wallet

    def _newWallet(self, walletName=None):
        nm = walletName or self.defaultWalletName

        while True:
            conflictFound = self._checkIfIdentifierConflicts(
                nm, checkInAliases=False, checkInSigners=False,
                printAppropriateMsg=False)
            if not conflictFound:
                break

            if walletName and conflictFound:
                raise NameAlreadyExists
            nm = "{}_{}".format(nm, randomString(5))

        if nm in self.wallets:
            self.print("Keyring {} already exists".format(nm))
            wallet = self._wallets[nm]
            self.activeWallet = wallet  # type: Wallet
            return wallet
        wallet = self._buildWalletClass(nm)
        self._wallets[nm] = wallet
        self.print("New keyring {} created".format(nm))
        self.activeWallet = wallet
        # TODO when the command is implemented
        # if nm == self.defaultWalletName:
        #     self.print("Note, you can rename this wallet by:")
        #     self.print("    rename wallet {} to NewName".format(nm))
        return wallet

    def _listKeyringsAction(self, matchedVars):
        if matchedVars.get('list_krs') == 'list keyrings':
            envs = self.getAllEnvDirNamesForKeyrings()
            contextDirPath = self.getContextBasedKeyringsBaseDir()
            envPaths = [os.path.join(self.getKeyringsBaseDir(), e) for e in envs]
            anyWalletFound = False
            for e in envPaths:
                fe = e.rstrip(os.sep)
                envName = basename(fe)
                files = glob.glob("{}/*.{}".format(fe, WALLET_FILE_EXTENSION))
                persistedWalletNames = []
                unpersistedWalletNames = []

                if len(files) > 0:
                    for f in files:
                        walletName = Cli.getWalletKeyName(basename(f))
                        persistedWalletNames.append(walletName)

                if contextDirPath == fe:
                    unpersistedWalletNames = [
                        n for n in self.wallets.keys()
                        if n.lower() not in persistedWalletNames]

                if len(persistedWalletNames) > 0 or \
                                len(unpersistedWalletNames) > 0:
                    anyWalletFound = True
                    self.print("\nEnvironment: {}".format(envName))

                if len(persistedWalletNames) > 0:
                    self.print("    Persisted wallets:")
                    for pwn in persistedWalletNames:
                        f = os.path.join(fe, Cli._normalizedWalletFileName(pwn))
                        lastModifiedTime = time.ctime(os.path.getmtime(f))
                        isThisActiveWallet = True if contextDirPath == fe and \
                               self._activeWallet is not None and \
                               self._activeWallet.name.lower() == pwn.lower() \
                            else False
                        activeKeyringMsg = " [Active keyring, may have some unsaved changes]" \
                            if isThisActiveWallet else ""
                        activeWalletSign = "*  " if isThisActiveWallet \
                            else "   "

                        self.print("    {}{}{}".format(
                            activeWalletSign, pwn, activeKeyringMsg), newline=False)
                        self.print(" (last modified at: {})".
                                   format(lastModifiedTime), Token.Gray)

                if len(unpersistedWalletNames) > 0:
                    self.print("    Un-persisted wallets:")
                    for n in unpersistedWalletNames:
                        self.print("        {}".format(n))

            if not anyWalletFound:
                self.print("No keyrings exists")

            return True

    def _listIdsAction(self, matchedVars):
        if matchedVars.get('list_ids') == 'list ids':
            if self._activeWallet:
                self.print("Active keyring: {}".
                           format(self._activeWallet.name), newline=False)
                if self._activeWallet.defaultId:
                    self.print(" (active identifier: {})\n".
                           format(self._activeWallet.defaultId), Token.Gray)
                if len(self._activeWallet.listIds()) > 0:
                    self.print("Identifiers:")
                    withVerkeys = matchedVars.get('with_verkeys') == 'with verkeys'
                    for id in self._activeWallet.listIds():
                        verKey = ""
                        if withVerkeys:
                            aliasId = self._activeWallet.aliasesToIds.get(id)
                            actualId = aliasId if aliasId else id
                            signer = self._activeWallet.idsToSigners.get(actualId)
                            verKey = ", verkey: {}".format(signer.verkey)

                        self.print("  {}{}".format(id, verKey))
                else:
                    self.print("\nNo identifiers")

            else:
                self.print("No active keyring found.")
            return True

    def checkIfPersistentWalletExists(self, name, inContextDir=None):
        toBeWalletFileName = Cli._normalizedWalletFileName(name)
        contextDir = inContextDir or self.getContextBasedKeyringsBaseDir()
        toBeWalletFilePath = Cli.getWalletFilePath(
            contextDir, toBeWalletFileName)
        if os.path.exists(toBeWalletFilePath):
            return toBeWalletFilePath

    def _checkIfIdentifierConflicts(self, origName, checkInWallets=True,
                                    checkInAliases=True, checkInSigners=True,
                                    printAppropriateMsg=True,
                                    checkPersistedFile=True):

        def _checkIfWalletExists(origName, checkInWallets=True,
                                 checkInAliases=True, checkInSigners=True,
                                 checkPersistedFile=True):
            if origName:
                name = origName.lower()
                allAliases = []
                allSigners = []
                allWallets = []

                for wk, wv in self.wallets.items():
                    if checkInAliases:
                        allAliases.extend(
                            [k.lower() for k in wv.aliasesToIds.keys()])
                    if checkInSigners:
                        allSigners.extend(list(wv.listIds()))
                    if checkInWallets:
                        allWallets.append(wk.lower())

                if name in allWallets:
                    return True, 'keyring'
                if name in allAliases:
                    return True, 'alias'
                if name in allSigners:
                    return True, 'identifier'

                if checkPersistedFile:
                    toBeWalletFilePath = self.checkIfPersistentWalletExists(origName)
                    if toBeWalletFilePath:
                        return True, 'keyring (stored at: {})'.\
                            format(toBeWalletFilePath)

                return False, None
            else:
                return False, None

        status, foundIn = _checkIfWalletExists(origName, checkInWallets,
                                               checkInAliases, checkInSigners,
                                               checkPersistedFile)
        if foundIn and printAppropriateMsg:
            self.print('"{}" conflicts with an existing {}. '
                       'Please choose a new name.'.
                       format(origName, foundIn), Token.Warning)
        return status

    def _loadWalletIfExistsAndNotLoaded(self, name, copyAs=None, override=False):
        wallet = self._getWalletByName(name)
        if not wallet:
            walletFileName = Cli._normalizedWalletFileName(name)
            self.restoreWalletByName(walletFileName, copyAs=copyAs,
                                     override=override)

    def _loadFromPath(self, path, copyAs=None, override=False):
        if os.path.exists(path):
            self.restoreWalletByPath(path, copyAs=copyAs, override=override)

    def _getWalletByName(self, name) -> Wallet:
        wallets = {k.lower(): v for k, v in self.wallets.items()}
        return wallets.get(name.lower())

    def checkIfWalletBelongsToCurrentContext(self, wallet):
        self.logger.debug("wallet context check: {}".format(wallet.name))
        self.logger.debug("  wallet.getEnvName: {}".format(wallet.getEnvName))
        self.logger.debug("  active env: {}".format(self.getActiveEnv))

        if wallet.getEnvName and wallet.getEnvName != self.getActiveEnv:
            self.logger.debug("  doesn't belong to the context")
            return False

        return True

    def _isWalletFilePathBelongsToCurrentContext(self, filePath):
        contextBasedKeyringsBaseDir = self.getContextBasedKeyringsBaseDir()
        fileBaseDir = dirname(filePath)

        self.logger.debug("wallet file path: {}".format(filePath))
        self.logger.debug("  contextBasedKeyringsBaseDir: {}".
                          format(contextBasedKeyringsBaseDir))
        self.logger.debug("  fileBaseDir: {}".format(fileBaseDir))

        if contextBasedKeyringsBaseDir != fileBaseDir:
            self.logger.debug("  doesn't belong to the context")
            return False

        return True

    def getAllEnvDirNamesForKeyrings(self):
        return [NO_ENV]

    def checkIfWalletPathBelongsToCurrentContext(self, filePath):
        keyringsBaseDir = self.getKeyringsBaseDir()
        baseWalletDirName = dirname(filePath)
        if not self._isWalletFilePathBelongsToCurrentContext(filePath):
            self.print("\nKeyring base directory is: {}"
                       "\nGiven keyring file {} "
                       "should be in one of it's sub directories "
                       "(you can create it if it doesn't exists) "
                       "according to the environment it belongs to."
                       "\nPossible sub directory names are: {}".
                       format(keyringsBaseDir, filePath,
                              self.getAllEnvDirNamesForKeyrings()))
            return False

        curContextDirName = self.getContextBasedKeyringsBaseDir()
        if baseWalletDirName != curContextDirName:
            self.print(
                self.getWalletFileIncompatibleForGivenContextMsg(filePath))
            return False

        return True

    def getWalletFileIncompatibleForGivenContextMsg(self, filePath):
        noEnvKeyringsBaseDir = self.getNoEnvKeyringsBaseDir()
        baseWalletDirName = dirname(filePath)
        msg = "Given wallet file ({}) doesn't belong to current context.".\
                format(filePath)
        if baseWalletDirName == noEnvKeyringsBaseDir:
            msg += "\nPlease disconnect and try again."
        else:
            msg += "\nPlease connect to '{}' environment and try again.".\
                format(basename(baseWalletDirName))
        return msg

    def _searchAndSetWallet(self, name, copyAs=None, override=False):
        if self._activeWallet and self._activeWallet.name.lower() == name.lower():
            self.print("Keyring already in use.")
            return True

        if os.path.isabs(name) and os.path.exists(name):
            self._loadFromPath(name, copyAs=copyAs, override=override)
        else:
            self._loadWalletIfExistsAndNotLoaded(name, copyAs=copyAs,
                                                 override=override)
            wallet = self._getWalletByName(name)
            if wallet and self._activeWallet.name != wallet.name:
                self._saveActiveWallet()
                self.activeWallet = wallet
            if not wallet:
                self.print("No such keyring found in current context.")
        return True

    def _saveKeyringAction(self, matchedVars):
        if matchedVars.get('save_kr') == 'save keyring':
            name = matchedVars.get('keyring')
            if not self._activeWallet:
                self.print("No active wallet to be saved.\n")
                return True

            if name:
                wallet = self._getWalletByName(name)
                if not wallet:
                    self.print("No such keyring loaded or exists.")
                    return True
                elif wallet.name != self._activeWallet.name:
                    self.print("Given keyring is not active "
                               "and it must be already saved.")
                    return True

            self._saveActiveWallet()
            return True

    def _useKeyringAction(self, matchedVars):
        if matchedVars.get('use_kr') == 'use keyring':
            name = matchedVars.get('keyring')
            override = True if matchedVars.get('override') else False
            copyAs = matchedVars.get('copy_as_name')
            self._searchAndSetWallet(name, copyAs=copyAs, override=override)
            return True

    def _setActiveIdentifier(self, idrOrAlias):
        if self.activeWallet:
            wallet = self.activeWallet
            if idrOrAlias not in wallet.aliasesToIds and \
                            idrOrAlias not in wallet.idsToSigners:
                return False
            idrFromAlias = wallet.aliasesToIds.get(idrOrAlias)
            # If alias found
            if idrFromAlias:
                self.activeIdentifier = idrFromAlias
                self.activeAlias = idrOrAlias
            else:
                alias = [k for k, v
                         in wallet.aliasesToIds.items()
                         if v == idrOrAlias]
                self.activeAlias = alias[0] if alias else None
                self.activeIdentifier = idrOrAlias
            wallet.defaultId = self.activeIdentifier
            self.print("Current identifier set to {}".
                       format(self.activeAlias or self.activeIdentifier))
            return True
        return False

    def _useIdentifierAction(self, matchedVars):
        if matchedVars.get('use_id') == 'use identifier':
            nymOrAlias = matchedVars.get('identifier')
            found = self._setActiveIdentifier(nymOrAlias)
            if not found:
                self.print("No such identifier found in current keyring")
            return True

    def _setPrompt(self, promptText):
        app = create_prompt_application('{}> '.format(promptText),
                                        lexer=self.grammarLexer,
                                        completer=self.grammarCompleter,
                                        style=self.style,
                                        history=self.pers_hist)
        self.cli.application = app
        self.currPromptText = promptText
        # getTokens = lambda _: [(Token.Prompt, promptText + "> ")]
        # self.cli.application.layout.children[1].children[0]\
        #     .content.content.get_tokens = getTokens

    def performEnvCompatibilityCheck(self, wallet, walletFilePath):
        if not self.checkIfWalletBelongsToCurrentContext(wallet):
            self.print(self.getWalletFileIncompatibleForGivenContextMsg(
                walletFilePath))
            return False

        if not self.checkIfWalletPathBelongsToCurrentContext(walletFilePath):
            return False

        return True

    @property
    def getWalletContextMistmatchMsg(self):
        return "The active keyring '{}' doesn't belong to current " \
               "environment. \nBefore you perform any transaction signing, " \
               "please create or activate compatible keyring.".\
            format(self._activeWallet.name)

    def printWarningIfIncompatibleWalletIsRestored(self, walletFilePath):
        if not self.checkIfWalletBelongsToCurrentContext(self._activeWallet) \
                or not self._isWalletFilePathBelongsToCurrentContext(walletFilePath):
            self.print(self.getWalletContextMistmatchMsg)
            self.print("Any changes made to this keyring won't be persisted.",
                       Token.BoldOrange)

    def performValidationCheck(self, wallet, walletFilePath, override=False):

        if not self.performEnvCompatibilityCheck(wallet, walletFilePath):
            return False

        conflictFound = self._checkIfIdentifierConflicts(
            wallet.name, checkInAliases=False, checkInSigners=False,
            checkPersistedFile=False, printAppropriateMsg=False)

        if conflictFound and not override:
            self.print(
                "A keyring with given name already loaded, "
                "here are few options:\n"
                "1. If you still want to load given persisted keyring at the "
                "risk of overriding the already loaded keyring, then add this "
                "clause to same command and retry: override\n"
                "2. If you want to create a copy of persisted keyring with "
                "different name, then, add this clause to "
                "same command and retry: copy-as <new-wallet-name>")
            return False

        return True

    def restoreWalletByPath(self, walletFilePath, copyAs=None, override=False):
        try:

            with open(walletFilePath) as walletFile:
                try:
                    # if wallet already exists, deserialize it
                    # and set as active wallet
                    wallet = decode(walletFile.read())
                    if copyAs:
                        wallet.name=copyAs

                    if not self.performValidationCheck(wallet, walletFilePath,
                                                       override):
                        return False

                    # As the persisted wallet restored and validated successfully,
                    # before we restore it, lets save active wallet (if exists)
                    if self._activeWallet:
                        self._saveActiveWallet()

                    self._wallets[wallet.name] = wallet
                    self.print('\nSaved keyring "{}" restored'.
                               format(wallet.name), newline=False)
                    self.print(" ({})".format(walletFilePath)
                               , Token.Gray)
                    self.activeWallet = wallet
                    self.activeIdentifier = wallet.defaultId

                    self.printWarningIfIncompatibleWalletIsRestored(walletFilePath)

                except (ValueError, AttributeError) as e:
                    self.logger.info(
                        "error occurred while restoring wallet {}: {}".
                            format(walletFilePath, e), Token.BoldOrange)
        except IOError:
            self.logger.debug("No such keyring file exists ({})".
                              format(walletFilePath))

    def restoreLastActiveWallet(self):
        filePattern = "*.{}".format(WALLET_FILE_EXTENSION)
        baseFileName=None
        try:
            def getLastModifiedTime(file):
                return os.stat(file).st_mtime_ns

            keyringPath = self.getContextBasedKeyringsBaseDir()
            newest = max(glob.iglob('{}/{}'.format(keyringPath, filePattern)),
                         key=getLastModifiedTime)
            baseFileName = basename(newest)
            self._searchAndSetWallet(os.path.join(keyringPath, baseFileName))
        except ValueError as e:
            if not str(e) == "max() arg is an empty sequence":
               self.errorDuringRestoringLastActiveWallet(baseFileName, e)
        except Exception as e:
            self.errorDuringRestoringLastActiveWallet(baseFileName, e)

    def errorDuringRestoringLastActiveWallet(self, baseFileName, e):
        self.logger.warning("Error occurred during restoring last "
                            "active wallet ({}), error: {}".
                            format(baseFileName, str(e)))
        raise e

    def restoreWalletByName(self, walletFileName, copyAs=None, override=False):
        walletFilePath = self.getWalletFilePath(
            self.getContextBasedKeyringsBaseDir(), walletFileName)
        self.restoreWalletByPath(walletFilePath, copyAs=copyAs, override=override)

    @staticmethod
    def getWalletKeyName(walletFileName):
        return walletFileName.replace(
            ".{}".format(WALLET_FILE_EXTENSION), "")

    @staticmethod
    def _normalizedWalletFileName(walletName):
        return "{}.{}".format(walletName.lower(), WALLET_FILE_EXTENSION)

    @staticmethod
    def getPromptAndEnv(cliName, currPromptText):
        if PROMPT_ENV_SEPARATOR not in currPromptText:
            return cliName, NO_ENV
        else:
            return currPromptText.rsplit(PROMPT_ENV_SEPARATOR, 1)

    def getActiveWalletPersitentFileName(self):
        fileName = self._activeWallet.name if self._activeWallet \
            else self.name
        return Cli._normalizedWalletFileName(fileName)


    @property
    def walletFileName(self):
        return self.getActiveWalletPersitentFileName()

    def getNoEnvKeyringsBaseDir(self):
        return os.path.expanduser(
            os.path.join(self.getKeyringsBaseDir(), NO_ENV))

    def getKeyringsBaseDir(self):
        return os.path.expanduser(os.path.join(self.config.baseDir,
                                        self.config.keyringsDir))

    def getContextBasedKeyringsBaseDir(self):
        keyringsBaseDir = self.getKeyringsBaseDir()
        prompt, envName = Cli.getPromptAndEnv(self.name,
                                              self.currPromptText)
        envKeyringsDir = keyringsBaseDir
        if envName != "":
            envKeyringsDir = os.path.join(keyringsBaseDir, envName)

        return envKeyringsDir

    def isAnyWalletFileExistsForGivenEnv(self, env):
        keyringPath = self.getKeyringsBaseDir()
        envKeyringPath = os.path.join(keyringPath, env)
        pattern = "{}/*.{}".format(envKeyringPath, WALLET_FILE_EXTENSION)
        return self.isAnyWalletFileExistsForGivenContext(pattern)

    def isAnyWalletFileExistsForGivenContext(self, pattern):
        files = glob.glob(pattern)
        if files:
            return True
        else:
            return False

    def isAnyWalletFileExistsForCurrentContext(self):
        keyringPath = self.getContextBasedKeyringsBaseDir()
        pattern = "{}/*.{}".format(keyringPath, WALLET_FILE_EXTENSION)
        return self.isAnyWalletFileExistsForGivenContext(pattern)

    @staticmethod
    def getWalletFilePath(basedir, walletFileName):
        return os.path.join(basedir, walletFileName)

    @property
    def getActiveEnv(self):
        return None

    def updateEnvNameInWallet(self):
        pass

    def performCompatibilityCheckBeforeSave(self):
        if self._activeWallet.getEnvName != self.getActiveEnv:
            walletEnvName = self._activeWallet.getEnvName \
                if self._activeWallet.getEnvName else "a different"
            currEnvName = " ({})".format(self.getActiveEnv) \
                if self.getActiveEnv else ""
            self.print("Active keyring belongs to '{}' environment and can't "
                       "be saved to the current environment{}.".
                       format(walletEnvName, currEnvName),
                       Token.BoldOrange)
            return False
        return True

    def _saveActiveWalletInDir(self, contextDir, printMsgs=True):
        try:
            createDirIfNotExists(contextDir)
            walletFilePath = Cli.getWalletFilePath(
                contextDir, self.walletFileName)
            with open(walletFilePath, "w+") as walletFile:
                try:
                    encodedWallet = encode(self._activeWallet)
                    walletFile.write(encodedWallet)
                    if printMsgs:
                        self.print('Active keyring "{}" saved'.format(
                            self._activeWallet.name), newline=False)
                        self.print(' ({})'.format(walletFilePath), Token.Gray)
                except ValueError as ex:
                    self.logger.info("ValueError: " +
                                     "Could not save wallet while exiting\n {}"
                                     .format(ex))
                except IOError:
                    self.logger.info(
                        "IOError while writing data to wallet file"
                    )
        except IOError as ex:
            self.logger.info("Error occurred while creating wallet. " +
                             "error no.{}, error.{}"
                             .format(ex.errno, ex.strerror))

    def _saveActiveWallet(self):
        if self._activeWallet:
            # We would save wallet only if user already has a wallet
            # otherwise our access for `activeWallet` property
            # will create a wallet
            self.updateEnvNameInWallet()
            if not self.performCompatibilityCheckBeforeSave():
                return False
            keyringsDir = self.getContextBasedKeyringsBaseDir()
            self._saveActiveWalletInDir(keyringsDir, printMsgs=True)

    def parse(self, cmdText):
        cmdText = cmdText.strip()
        m = self.grammar.match(cmdText)
        # noinspection PyProtectedMember
        if m and len(m.variables()._tuples):
            matchedVars = m.variables()
            self.logger.info("CLI command entered: {}".format(cmdText),
                             extra={"cli": False})
            for action in self.actions:
                r = action(matchedVars)
                if r:
                    break
            else:
                self.invalidCmd(cmdText)
        else:
            if cmdText != "":
                self.invalidCmd(cmdText)

    @staticmethod
    def createEntities(name: str, moreNames: str, matchedVars, initializer):
        entity = matchedVars.get(name)
        more = matchedVars.get(moreNames)
        more = more.split(',') if more is not None and len(more) > 0 else []
        names = [n for n in [entity] + more if len(n) != 0]
        seed = matchedVars.get("seed")
        identifier = matchedVars.get("nym")
        if len(names) == 1 and (seed or identifier):
            initializer(names[0].strip(), seed=seed, identifier=identifier)
        else:
            for name in names:
                initializer(name.strip())

    def invalidCmd(self, cmdText):
        matchedHelpMsg = self.getMatchedHelpableMsg(cmdText)
        if matchedHelpMsg:
            self.print("Invalid syntax: '{}'".format(cmdText))
            self.print(str(matchedHelpMsg))
        else:
            self.print("Invalid command: '{}'".format(cmdText))
            self.printHelp()

    # def nextAvailableClientAddr(self, curClientPort=8100):
    #     self.curClientPort = self.curClientPort or curClientPort
    #     # TODO: Find a better way to do this
    #     self.curClientPort += random.randint(1, 200)
    #     host = "0.0.0.0"
    #     try:
    #         checkPortAvailable((host, self.curClientPort))
    #         assert not isPortUsed(self.basedirpath, self.curClientPort), \
    #             "Port used by a remote"
    #         return host, self.curClientPort
    #     except Exception as ex:
    #         tokens = [(Token.Error, "Cannot bind to port {}: {}, "
    #                                 "trying another port.\n".
    #                    format(self.curClientPort, ex))]
    #         self.printTokens(tokens)
    #         return self.nextAvailableClientAddr(self.curClientPort)

    @property
    def hasAnyKey(self):
        if not self._activeWallet or not self._activeWallet.defaultId:
            self.printNoKeyMsg()
            return False
        return True

    def printNoKeyMsg(self):
        self.print("No key present in keyring")
        self.printUsage(("new key [with seed <32 byte string>]", ))

    def printUsage(self, msgs):
        self.print("\nUsage:")
        for m in msgs:
            self.print('  {}'.format(m))
        self.print("\n")

    # TODO: Do we keep this? What happens when we allow the CLI to connect
    # to remote nodes?
    def cleanUp(self):
        dataPath = os.path.join(self.config.baseDir, "data")
        try:
            shutil.rmtree(dataPath, ignore_errors=True)
        except FileNotFoundError:
            pass

        client = pyorient.OrientDB(self.config.OrientDB["host"],
                                   self.config.OrientDB["port"])
        user = self.config.OrientDB["user"]
        password = self.config.OrientDB["password"]
        client.connect(user, password)

        def dropdbs():
            i = 0
            names = [n for n in
                     client.db_list().oRecordData['databases'].keys()]
            for nm in names:
                try:
                    client.db_drop(nm)
                    i += 1
                except:
                    continue
            return i

        dropdbs()

    def __hash__(self):
        return hash((self.name, self.unique_name, self.basedirpath))

    def __eq__(self, other):
        return (self.name, self.unique_name, self.basedirpath) == \
               (other.name, self.unique_name, other.basedirpath)


class Exit(Exception):
    pass


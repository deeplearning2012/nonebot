import re
import asyncio
from datetime import datetime
from typing import (
    Tuple, Union, Callable, Iterable, Dict, Any, Optional, Sequence
)

from aiocqhttp import CQHttp
from aiocqhttp.message import Message

from . import permission as perm
from .helpers import context_source
from .expression import render
from .session import BaseSession

# Key: str (one segment of command name)
# Value: subtree or a leaf Command object
_registry = {}

# Key: str
# Value: tuple that identifies a command
_aliases = {}

# Key: context source
# Value: Session object
_sessions = {}


class Command:
    __slots__ = ('name', 'func', 'permission', 'only_to_me', 'args_parser_func')

    def __init__(self, *, name: Tuple[str], func: Callable, permission: int,
                 only_to_me: bool):
        self.name = name
        self.func = func
        self.permission = permission
        self.only_to_me = only_to_me
        self.args_parser_func = None

    async def run(self, session, check_perm: bool = True) -> bool:
        """
        Run the command in a given session.

        :param session: CommandSession object
        :param check_perm: should check permission before running
        :return: the command is finished
        """
        if check_perm:
            has_perm = await perm.check_permission(
                session.bot, session.ctx, self.permission)
        else:
            has_perm = True
        if self.func and has_perm:
            if self.args_parser_func:
                await self.args_parser_func(session)
            await self.func(session)
            return True
        return False


def on_command(name: Union[str, Tuple[str]], *,
               aliases: Iterable = (),
               permission: int = perm.EVERYBODY,
               only_to_me: bool = True) -> Callable:
    def deco(func: Callable) -> Callable:
        if not isinstance(name, (str, tuple)):
            raise TypeError('the name of a command must be a str or tuple')
        if not name:
            raise ValueError('the name of a command must not be empty')

        cmd_name = name if isinstance(name, tuple) else (name,)
        current_parent = _registry
        for parent_key in cmd_name[:-1]:
            current_parent[parent_key] = current_parent.get(parent_key) or {}
            current_parent = current_parent[parent_key]
        cmd = Command(name=cmd_name, func=func, permission=permission,
                      only_to_me=only_to_me)
        current_parent[cmd_name[-1]] = cmd
        for alias in aliases:
            _aliases[alias] = cmd_name

        def args_parser_deco(parser_func: Callable):
            cmd.args_parser_func = parser_func
            return parser_func

        func.args_parser = args_parser_deco
        return func

    return deco


class CommandGroup:
    """
    Group a set of commands with same name prefix.
    """

    __slots__ = ('basename', 'permission', 'only_to_me')

    def __init__(self, name: Union[str, Tuple[str]],
                 permission: Optional[int] = None, *,
                 only_to_me: Optional[bool] = None):
        self.basename = (name,) if isinstance(name, str) else name
        self.permission = permission
        self.only_to_me = only_to_me

    def command(self, name: Union[str, Tuple[str]], *,
                aliases: Optional[Iterable] = None,
                permission: Optional[int] = None,
                only_to_me: Optional[bool] = None) -> Callable:
        sub_name = (name,) if isinstance(name, str) else name
        name = self.basename + sub_name

        kwargs = {}
        if aliases is not None:
            kwargs['aliases'] = aliases
        if permission is not None:
            kwargs['permission'] = permission
        elif self.permission is not None:
            kwargs['permission'] = self.permission
        if only_to_me is not None:
            kwargs['only_to_me'] = only_to_me
        elif self.only_to_me is not None:
            kwargs['only_to_me'] = self.only_to_me
        return on_command(name, **kwargs)


def _find_command(name: Union[str, Tuple[str]]) -> Optional[Command]:
    cmd_name = (name,) if isinstance(name, str) else name
    if not cmd_name:
        return None

    cmd_tree = _registry
    for part in cmd_name[:-1]:
        if part not in cmd_tree:
            return None
        cmd_tree = cmd_tree[part]

    return cmd_tree.get(cmd_name[-1])


class _FurtherInteractionNeeded(Exception):
    """
    Raised by session.require_arg() indicating
    that the command should enter interactive mode
    to ask the user for some arguments.
    """
    pass


class CommandSession(BaseSession):
    __slots__ = ('cmd', 'current_key', 'current_arg', 'current_arg_text',
                 'current_arg_images', 'args', 'last_interaction')

    def __init__(self, bot: CQHttp, ctx: Dict[str, Any], cmd: Command, *,
                 current_arg: str = '', args: Optional[Dict[str, Any]] = None):
        super().__init__(bot, ctx)
        self.cmd = cmd
        self.current_key = None
        self.current_arg = None
        self.current_arg_text = None
        self.current_arg_images = None
        self.refresh(ctx, current_arg=current_arg)
        self.args = args or {}
        self.last_interaction = None

    def refresh(self, ctx: Dict[str, Any], *, current_arg: str = '') -> None:
        """
        Refill the session with a new message context.

        :param ctx: new message context
        :param current_arg: new command argument as a string
        """
        self.ctx = ctx
        self.current_arg = current_arg
        current_arg_as_msg = Message(current_arg)
        self.current_arg_text = current_arg_as_msg.extract_plain_text()
        self.current_arg_images = [s.data['url'] for s in current_arg_as_msg
                                   if s.type == 'image' and 'url' in s.data]

    @property
    def is_valid(self) -> bool:
        """Check if the session is expired or not."""
        if self.last_interaction and \
                datetime.now() - self.last_interaction > \
                self.bot.config.SESSION_EXPIRE_TIMEOUT:
            return False
        return True

    def get(self, key: str, *, prompt: str = None,
            prompt_expr: Union[str, Sequence[str], Callable] = None) -> Any:
        """
        Get an argument with a given key.

        If the argument does not exist in the current session,
        a FurtherInteractionNeeded exception will be raised,
        and the caller of the command will know it should keep
        the session for further interaction with the user.

        :param key: argument key
        :param prompt: prompt to ask the user
        :param prompt_expr: prompt expression to ask the user
        :return: the argument value
        :raise FurtherInteractionNeeded: further interaction is needed
        """
        value = self.get_optional(key)
        if value is not None:
            return value

        self.current_key = key
        # ask the user for more information
        if prompt_expr is not None:
            prompt = render(prompt_expr, key=key)
        if prompt:
            asyncio.ensure_future(self.send(prompt))
        raise _FurtherInteractionNeeded

    def get_optional(self, key: str,
                     default: Optional[Any] = None) -> Optional[Any]:
        return self.args.get(key, default)


def _new_command_session(bot: CQHttp,
                         ctx: Dict[str, Any]) -> Optional[CommandSession]:
    """
    Create a new session for a command.

    This will firstly attempt to parse the current message as
    a command, and if succeeded, it then create a session for
    the command and return. If the message is not a valid command,
    None will be returned.

    :param bot: CQHttp instance
    :param ctx: message context
    :return: CommandSession object or None
    """
    msg_text = str(ctx['message']).lstrip()

    for start in bot.config.COMMAND_START:
        if isinstance(start, type(re.compile(''))):
            m = start.search(msg_text)
            if m:
                full_command = msg_text[len(m.group(0)):].lstrip()
                break
        elif isinstance(start, str):
            if msg_text.startswith(start):
                full_command = msg_text[len(start):].lstrip()
                break
    else:
        # it's not a command
        return None

    if not full_command:
        # command is empty
        return None

    cmd_name_text, *cmd_remained = full_command.split(maxsplit=1)
    cmd_name = _aliases.get(cmd_name_text)

    if not cmd_name:
        for sep in bot.config.COMMAND_SEP:
            if isinstance(sep, type(re.compile(''))):
                cmd_name = tuple(sep.split(cmd_name_text))
                break
            elif isinstance(sep, str):
                cmd_name = tuple(cmd_name_text.split(sep))
                break
        else:
            cmd_name = (cmd_name_text,)

    cmd = _find_command(cmd_name)
    if not cmd:
        return None
    if cmd.only_to_me and not ctx['to_me']:
        return None

    return CommandSession(bot, ctx, cmd, current_arg=''.join(cmd_remained))


async def handle_command(bot: CQHttp, ctx: Dict[str, Any]) -> bool:
    """
    Handle a message as a command.

    This function is typically called by "handle_message".

    :param bot: CQHttp instance
    :param ctx: message context
    :return: the message is handled as a command
    """
    src = context_source(ctx)
    session = None
    check_perm = True
    if _sessions.get(src):
        session = _sessions[src]
        if session and session.is_valid:
            session.refresh(ctx, current_arg=str(ctx['message']))
            # there is no need to check permission for existing session
            check_perm = False
        else:
            # the session is expired, remove it
            del _sessions[src]
            session = None
    if not session:
        session = _new_command_session(bot, ctx)
        if not session:
            return False

    return await _real_run_command(session, src, check_perm=check_perm)


async def call_command(bot: CQHttp, ctx: Dict[str, Any],
                       name: Union[str, Tuple[str]],
                       args: Dict[str, Any]) -> bool:
    """
    Call a command internally.

    This function is typically called by some other commands
    or "handle_natural_language" when handling NLPResult object.

    :param bot: CQHttp instance
    :param ctx: message context
    :param name: command name
    :param args: command args
    :return: the command is successfully called
    """
    cmd = _find_command(name)
    if not cmd:
        return False
    session = CommandSession(bot, ctx, cmd, args=args)
    return await _real_run_command(session, context_source(session.ctx),
                                   check_perm=False)


async def _real_run_command(session: CommandSession,
                            ctx_src: str, **kwargs) -> bool:
    _sessions[ctx_src] = session
    try:
        res = await session.cmd.run(session, **kwargs)
        # the command is finished, remove the session
        del _sessions[ctx_src]
        return res
    except _FurtherInteractionNeeded:
        session.last_interaction = datetime.now()
        # return True because this step of the session is successful
        return True

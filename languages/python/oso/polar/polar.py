"""Communicate with the Polar virtual machine: load rules, make queries, etc."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from typing import Dict, List, Union

try:
    # importing readline on compatible platforms
    # changes how `input` works for the REPL
    import readline  # noqa: F401
except ImportError:
    pass

from .exceptions import (
    PolarRuntimeError,
    InlineQueryFailedError,
    ParserError,
    PolarFileExtensionError,
    PolarFileNotFoundError,
    InvalidQueryTypeError,
)
from .ffi import Polar as FfiPolar, PolarSource as Source
from .host import Host
from .query import Query
from .predicate import Predicate
from .variable import Variable
from .expression import Expression, Pattern
from .data_filtering import serialize_types, filter_data, Relation


# https://github.com/django/django/blob/3e753d3de33469493b1f0947a2e0152c4000ed40/django/core/management/color.py
def supports_color():
    supported_platform = sys.platform != "win32" or "ANSICON" in os.environ
    is_a_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    return supported_platform and is_a_tty


RESET = ""
FG_BLUE = ""
FG_RED = ""


if supports_color():
    # \001 and \002 signal these should be ignored by readline. Explanation of
    # the issue: https://stackoverflow.com/a/9468954/390293. Issue has been
    # observed in the Python REPL on Linux by @samscott89 and @plotnick, but
    # not on macOS or Windows (with readline installed) or in the Ruby or
    # Node.js REPLs, both of which also use readline.
    RESET = "\001\x1b[0m\002"
    FG_BLUE = "\001\x1b[34m\002"
    FG_RED = "\001\x1b[31m\002"


def print_error(error):
    print(FG_RED + type(error).__name__ + RESET)
    print(error)


CLASSES: Dict[str, type] = {}


class Polar:
    """Polar API"""

    def __init__(self, classes=CLASSES):
        self.ffi_polar = FfiPolar()
        self.host = Host(self.ffi_polar)
        self.ffi_polar.set_message_enricher(self.host.enrich_message)

        # Register global constants.
        self.register_constant(None, name="nil")

        # Register built-in classes.
        self.register_class(bool, name="Boolean")
        self.register_class(int, name="Integer")
        self.register_class(float, name="Float")
        self.register_class(list, name="List")
        self.register_class(dict, name="Dictionary")
        self.register_class(str, name="String")
        self.register_class(datetime, name="Datetime")
        self.register_class(timedelta, name="Timedelta")

        # Pre-registered classes.
        for name, cls in classes.items():
            self.register_class(cls, name=name)

    def __del__(self):
        del self.host
        del self.ffi_polar

    def load_files(self, filenames: List[Union[Path, str]] = []):
        """Load Polar policy from ".polar" files."""
        if not filenames:
            return

        sources: List[Source] = []

        for filename in filenames:
            path = Path(filename)
            extension = path.suffix
            filename = str(path)
            if not extension == ".polar":
                raise PolarFileExtensionError(filename)

            try:
                with open(filename, "rb") as f:
                    src = f.read().decode("utf-8")
                    sources.append(Source(src, filename))
            except FileNotFoundError:
                raise PolarFileNotFoundError(filename)

        self._load_sources(sources)

    def load_file(self, filename: Union[Path, str]):
        """Load Polar policy from a ".polar" file."""
        print(
            "`Oso.load_file` has been deprecated in favor of `Oso.load_files` as of the 0.20.0 release.\n\n"
            + "Please see changelog for migration instructions: https://docs.osohq.com/project/changelogs/2021-09-15.html",
            file=sys.stderr,
        )
        self.load_files([filename])

    def load_str(self, string: str):
        """Load a Polar string, checking that all inline queries succeed."""
        # NOTE: not ideal that the MRO gets updated each time load_str is
        # called, but since we are planning to move to only calling load once
        # with the include feature, I think it's okay for now.
        self._load_sources([Source(string)])

    # Register MROs, load Polar code, and check inline queries.
    def _load_sources(self, sources: List[Source]):
        self.host.register_mros()
        self.ffi_polar.load(sources)
        self.check_inline_queries()

    def check_inline_queries(self):
        while True:
            query = self.ffi_polar.next_inline_query()
            if query is None:  # Load is done
                break
            else:
                try:
                    next(Query(query, host=self.host.copy()).run())
                except StopIteration:
                    source = query.source()
                    raise InlineQueryFailedError(source.get())

    def clear_rules(self):
        self.ffi_polar.clear_rules()

    def query(self, query, *, bindings=None, accept_expression=False):
        """Query for a predicate, parsing it if necessary.

        :param query: The predicate to query for.

        :return: The result of the query.
        """
        host = self.host.copy()
        host.set_accept_expression(accept_expression)

        if isinstance(query, str):
            query = self.ffi_polar.new_query_from_str(query)
        elif isinstance(query, Predicate):
            query = self.ffi_polar.new_query_from_term(host.to_polar(query))
        else:
            raise InvalidQueryTypeError()

        for res in Query(query, host=host, bindings=bindings).run():
            yield res

    def query_rule(self, name, *args, **kwargs):
        """Query for rule with name ``name`` and arguments ``args``.

        :param name: The name of the predicate to query.
        :param args: Arguments for the predicate.

        :return: The result of the query.
        """
        return self.query(Predicate(name=name, args=args), **kwargs)

    def query_rule_once(self, name, *args, **kwargs):
        """Check a rule with name ``name`` and arguments ``args``.

        :param name: The name of the predicate to query.
        :param args: Arguments for the predicate.

        :return: True if the query has any results, False otherwise.
        """
        try:
            next(self.query(Predicate(name=name, args=args), **kwargs))
            return True
        except StopIteration:
            return False

    def repl(self, files=[]):
        """Start an interactive REPL session."""
        self.load_files(files)

        while True:
            try:
                query = input(FG_BLUE + "query> " + RESET).strip(";")
            except (EOFError, KeyboardInterrupt):
                return
            try:
                ffi_query = self.ffi_polar.new_query_from_str(query)
            except ParserError as e:
                print_error(e)
                continue

            host = self.host.copy()
            host.set_accept_expression(True)
            result = False
            try:
                query = Query(ffi_query, host=host).run()
                for res in query:
                    result = True
                    bindings = res["bindings"]
                    if bindings:
                        for variable, value in bindings.items():
                            print(variable + " = " + repr(value))
                    else:
                        print(True)
            except PolarRuntimeError as e:
                print_error(e)
                continue
            if not result:
                print(False)

    def register_class(
        self,
        cls,
        *,
        name=None,
        types=None,
        build_query=None,
        exec_query=None,
        combine_query=None
    ):
        """Register `cls` as a class accessible by Polar."""
        # TODO: let's add example usage here or at least a proper docstring for the arguments
        cls_name = self.host.cache_class(
            cls,
            name=name,
            fields=types,
            build_query=build_query,
            exec_query=exec_query,
            combine_query=combine_query,
        )
        self.register_constant(cls, cls_name)

    def register_constant(self, value, name):
        """Register `value` as a Polar constant variable called `name`."""
        self.ffi_polar.register_constant(self.host.to_polar(value), name)

    def get_class(self, name):
        """Return class registered for ``name``.

        :raises UnregisteredClassError: If the class is not registered.
        """
        return self.host.get_class(name)

    def authorized_query(self, actor, action, cls):
        """
        Returns a query for the resources the actor is allowed to perform action on.
        The query is built by using the build_query and combine_query methods registered for the type.

        :param actor: The actor for whom to collect allowed resources.

        :param action: The action that user wants to perform.

        :param cls: The type of the resources.

        :return: A query to fetch the resources,
        """
        # Data filtering.
        resource = Variable("resource")
        # Get registered class name somehow
        class_name = self.host.types[cls].name
        constraint = Expression(
            "And", [Expression("Isa", [resource, Pattern(class_name, {})])]
        )
        results = list(
            self.query_rule(
                "allow",
                actor,
                action,
                resource,
                bindings={"resource": constraint},
                accept_expression=True,
            )
        )

        # @TODO: How do you deal with value results in the query case?
        # Do we get them into the filter plan as constraints somehow?
        complete, partial = [], []

        for result in results:
            for k, v in result["bindings"].items():
                if isinstance(v, Expression):
                    partial.append({"bindings": {k: self.host.to_polar(v)}})
                else:
                    complete.append(v)

        types = serialize_types(self.host.distinct_user_types(), self.host.types)
        plan = self.ffi_polar.build_filter_plan(types, partial, "resource", class_name)

        # A little tbd if this should happen here or in build_filter_plan.
        # Would have to wrap them in bindings probably to pass into build_filter_plan
        if len(complete) > 0:
            new_result_sets = []
            for c in complete:
                constraints = []
                typ = self.host.types[class_name]
                if not typ.build_query:
                    # Maybe a way around this if we make builtins for our builtin
                    # classes but it'd be a hack just for this case and not worth it right now.
                    assert False, "Can only filter registered classes"

                for k, t in typ.fields.items():
                    if not isinstance(t, Relation):
                        constraint = {
                            "kind": "Eq",
                            "field": k,
                            "value": {"Term": self.host.to_polar(getattr(c, k))},
                        }
                        constraints.append(constraint)

                result_set = {
                    "requests": {
                        "0": {"class_tag": class_name, "constraints": constraints}
                    },
                    "resolve_order": [0],
                    "result_id": 0,
                }
                new_result_sets.append(result_set)
            plan["result_sets"] += new_result_sets

        return filter_data(self, plan)

    def authorized_resources(self, actor, action, cls):
        query = self.authorized_query(actor, action, cls)
        if query is None:
            return []

        results = self.host.types[cls].exec_query(query)
        return results


def polar_class(_cls=None, *, name=None):
    """Decorator to register a Python class with Polar.
    An alternative to ``register_class()``."""

    def wrap(cls):
        cls_name = cls.__name__ if name is None else name
        CLASSES[cls_name] = cls
        return cls

    if _cls is None:
        return wrap

    return wrap(_cls)

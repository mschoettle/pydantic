from __future__ import annotations

import sys
from configparser import ConfigParser
from typing import Any, Callable

from mypy.errorcodes import ErrorCode
from mypy.nodes import (
    ARG_NAMED,
    ARG_NAMED_OPT,
    ARG_OPT,
    ARG_POS,
    ARG_STAR2,
    MDEF,
    Argument,
    AssignmentStmt,
    Block,
    CallExpr,
    ClassDef,
    Context,
    Decorator,
    EllipsisExpr,
    Expression,
    FuncBase,
    FuncDef,
    JsonDict,
    MemberExpr,
    NameExpr,
    PassStmt,
    PlaceholderNode,
    RefExpr,
    Statement,
    StrExpr,
    SymbolNode,
    SymbolTableNode,
    TempNode,
    TypeInfo,
    TypeVarExpr,
    Var,
)
from mypy.options import Options
from mypy.plugin import (
    CheckerPluginInterface,
    ClassDefContext,
    FunctionContext,
    MethodContext,
    Plugin,
    ReportConfigContext,
    SemanticAnalyzerPluginInterface,
)
from mypy.plugins import dataclasses
from mypy.semanal import set_callable_name
from mypy.server.trigger import make_wildcard_trigger
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    NoneType,
    Overloaded,
    Type,
    TypeOfAny,
    TypeType,
    TypeVarType,
    UnionType,
    get_proper_type,
)
from mypy.typevars import fill_typevars
from mypy.util import get_unique_redefinition_name
from mypy.version import __version__ as mypy_version

try:
    from mypy.types import TypeVarDef  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    # Backward-compatible with TypeVarDef from Mypy 0.930.
    from mypy.types import TypeVarType as TypeVarDef

CONFIGFILE_KEY = 'pydantic-mypy'
METADATA_KEY = 'pydantic-mypy-metadata'
BASEMODEL_FULLNAME = 'pydantic.main.BaseModel'
MODEL_METACLASS_FULLNAME = 'pydantic._internal._model_construction.ModelMetaclass'
FIELD_FULLNAME = 'pydantic.fields.Field'
DATACLASS_FULLNAME = 'pydantic.dataclasses.dataclass'
DECORATOR_FULLNAMES = {
    'pydantic.functional_validators.field_validator',
    'pydantic.functional_validators.model_validator',
    'pydantic.functional_serializers.serializer',
    'pydantic.functional_serializers.model_serializer',
    'pydantic.deprecated.class_validators.validator',
    'pydantic.deprecated.class_validators.root_validator',
}


def parse_mypy_version(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.partition('+')[0].split('.')))


MYPY_VERSION_TUPLE = parse_mypy_version(mypy_version)
BUILTINS_NAME = 'builtins' if MYPY_VERSION_TUPLE >= (0, 930) else '__builtins__'

# Increment version if plugin changes and mypy caches should be invalidated
__version__ = 2


def plugin(version: str) -> type[Plugin]:
    """
    `version` is the mypy version string

    We might want to use this to print a warning if the mypy version being used is
    newer, or especially older, than we expect (or need).
    """
    return PydanticPlugin


class PydanticPlugin(Plugin):
    def __init__(self, options: Options) -> None:
        self.plugin_config = PydanticPluginConfig(options)
        self._plugin_data = self.plugin_config.to_data()
        super().__init__(options)

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        sym = self.lookup_fully_qualified(fullname)
        if sym and isinstance(sym.node, TypeInfo):  # pragma: no branch
            # No branching may occur if the mypy cache has not been cleared
            if any(get_fullname(base) == BASEMODEL_FULLNAME for base in sym.node.mro):
                return self._pydantic_model_class_maker_callback
        return None

    def get_metaclass_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        if fullname == MODEL_METACLASS_FULLNAME:
            return self._pydantic_model_metaclass_marker_callback
        return None

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        sym = self.lookup_fully_qualified(fullname)
        if sym and sym.fullname == FIELD_FULLNAME:
            return self._pydantic_field_callback
        return None

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        if fullname.endswith('.from_orm'):
            return from_attributes_callback
        return None

    def get_class_decorator_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Mark pydantic.dataclasses as dataclass.

        Mypy version 1.1.1 added support for `@dataclass_transform` decorator.
        """
        if fullname == DATACLASS_FULLNAME and MYPY_VERSION_TUPLE < (1, 1):
            return dataclasses.dataclass_class_maker_callback  # type: ignore[return-value]
        return None

    def report_config_data(self, ctx: ReportConfigContext) -> dict[str, Any]:
        """Return all plugin config data.

        Used by mypy to determine if cache needs to be discarded.
        """
        return self._plugin_data

    def _pydantic_model_class_maker_callback(self, ctx: ClassDefContext) -> None:
        transformer = PydanticModelTransformer(ctx, self.plugin_config)
        transformer.transform()

    def _pydantic_model_metaclass_marker_callback(self, ctx: ClassDefContext) -> None:
        """Reset dataclass_transform_spec attribute of ModelMetaclass.

        Let the plugin handle it. This behavior can be disabled
        if 'debug_dataclass_transform' is set to True', for testing purposes.
        """
        if self.plugin_config.debug_dataclass_transform:
            return
        info_metaclass = ctx.cls.info.declared_metaclass
        assert info_metaclass, "callback not passed from 'get_metaclass_hook'"
        if getattr(info_metaclass.type, 'dataclass_transform_spec', None):
            info_metaclass.type.dataclass_transform_spec = None

    def _pydantic_field_callback(self, ctx: FunctionContext) -> Type:
        """
        Extract the type of the `default` argument from the Field function, and use it as the return type.

        In particular:
        * Check whether the default and default_factory argument is specified.
        * Output an error if both are specified.
        * Retrieve the type of the argument which is specified, and use it as return type for the function.
        """
        default_any_type = ctx.default_return_type

        assert ctx.callee_arg_names[0] == 'default', '"default" is no longer first argument in Field()'
        assert ctx.callee_arg_names[1] == 'default_factory', '"default_factory" is no longer second argument in Field()'
        default_args = ctx.args[0]
        default_factory_args = ctx.args[1]

        if default_args and default_factory_args:
            error_default_and_default_factory_specified(ctx.api, ctx.context)
            return default_any_type

        if default_args:
            default_type = ctx.arg_types[0][0]
            default_arg = default_args[0]

            # Fallback to default Any type if the field is required
            if not isinstance(default_arg, EllipsisExpr):
                return default_type

        elif default_factory_args:
            default_factory_type = ctx.arg_types[1][0]

            # Functions which use `ParamSpec` can be overloaded, exposing the callable's types as a parameter
            # Pydantic calls the default factory without any argument, so we retrieve the first item
            if isinstance(default_factory_type, Overloaded):
                default_factory_type = default_factory_type.items[0]

            if isinstance(default_factory_type, CallableType):
                ret_type = default_factory_type.ret_type
                # mypy doesn't think `ret_type` has `args`, you'd think mypy should know,
                # add this check in case it varies by version
                args = getattr(ret_type, 'args', None)
                if args:
                    if all(isinstance(arg, TypeVarType) for arg in args):
                        # Looks like the default factory is a type like `list` or `dict`, replace all args with `Any`
                        ret_type.args = tuple(default_any_type for _ in args)  # type: ignore[attr-defined]
                return ret_type

        return default_any_type


class PydanticPluginConfig:
    __slots__ = (
        'init_forbid_extra',
        'init_typed',
        'warn_required_dynamic_aliases',
        'debug_dataclass_transform',
    )
    init_forbid_extra: bool
    init_typed: bool
    warn_required_dynamic_aliases: bool
    debug_dataclass_transform: bool  # undocumented

    def __init__(self, options: Options) -> None:
        if options.config_file is None:  # pragma: no cover
            return

        toml_config = parse_toml(options.config_file)
        if toml_config is not None:
            config = toml_config.get('tool', {}).get('pydantic-mypy', {})
            for key in self.__slots__:
                setting = config.get(key, False)
                if not isinstance(setting, bool):
                    raise ValueError(f'Configuration value must be a boolean for key: {key}')
                setattr(self, key, setting)
        else:
            plugin_config = ConfigParser()
            plugin_config.read(options.config_file)
            for key in self.__slots__:
                setting = plugin_config.getboolean(CONFIGFILE_KEY, key, fallback=False)
                setattr(self, key, setting)

    def to_data(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__slots__}


def from_attributes_callback(ctx: MethodContext) -> Type:
    """
    Raise an error if from_attributes is not enabled
    """
    model_type: Instance
    ctx_type = ctx.type
    if isinstance(ctx_type, TypeType):
        ctx_type = ctx_type.item
    if isinstance(ctx_type, CallableType) and isinstance(ctx_type.ret_type, Instance):
        model_type = ctx_type.ret_type  # called on the class
    elif isinstance(ctx_type, Instance):
        model_type = ctx_type  # called on an instance (unusual, but still valid)
    else:  # pragma: no cover
        detail = f'ctx.type: {ctx_type} (of type {ctx_type.__class__.__name__})'
        error_unexpected_behavior(detail, ctx.api, ctx.context)
        return ctx.default_return_type
    pydantic_metadata = model_type.type.metadata.get(METADATA_KEY)
    if pydantic_metadata is None:
        return ctx.default_return_type
    from_attributes = pydantic_metadata.get('config', {}).get('from_attributes')
    if from_attributes is not True:
        error_from_attributes(get_name(model_type.type), ctx.api, ctx.context)
    return ctx.default_return_type


class PydanticModelTransformer:
    tracked_config_fields: set[str] = {
        'extra',
        'frozen',
        'from_attributes',
        'populate_by_name',
        'alias_generator',
    }

    def __init__(self, ctx: ClassDefContext, plugin_config: PydanticPluginConfig) -> None:
        self._ctx = ctx
        self.plugin_config = plugin_config

    def transform(self) -> None:
        """
        Configures the BaseModel subclass according to the plugin settings.

        In particular:
        * determines the model config and fields,
        * adds a fields-aware signature for the initializer and construct methods
        * freezes the class if frozen = True
        * stores the fields, config, and if the class is settings in the mypy metadata for access by subclasses
        """
        ctx = self._ctx
        info = ctx.cls.info

        self.adjust_validator_signatures()
        config = self.collect_config()
        fields = self.collect_fields(config)
        self.add_initializer(fields, config)
        self.add_model_construct_method(fields)
        self.set_frozen(fields, frozen=config.frozen is True)
        info.metadata[METADATA_KEY] = {
            'fields': {field.name: field.serialize() for field in fields},
            'config': config.set_values_dict(),
        }

    def adjust_validator_signatures(self) -> None:
        """
        When we decorate a function `f` with `pydantic.validator(...)`, `pydantic.field_validator`
        or `pydantic.serializer(...)`, mypy sees `f` as a regular method taking a `self` instance,
        even though pydantic internally wraps `f` with `classmethod` if necessary.

        Teach mypy this by marking any function whose outermost decorator is a `validator()`,
        `field_validator()` or `serializer()` call as a `classmethod`.
        """
        for name, sym in self._ctx.cls.info.names.items():
            if isinstance(sym.node, Decorator):
                first_dec = sym.node.original_decorators[0]
                if (
                    isinstance(first_dec, CallExpr)
                    and isinstance(first_dec.callee, NameExpr)
                    and first_dec.callee.fullname in DECORATOR_FULLNAMES
                ):
                    sym.node.func.is_class = True

    def collect_config(self) -> ModelConfigData:  # noqa: C901 (ignore complexity)
        """
        Collects the values of the config attributes that are used by the plugin, accounting for parent classes.
        """
        ctx = self._ctx
        cls = ctx.cls
        config = ModelConfigData()

        has_config_kwargs = False
        has_config_from_namespace = False

        for name, expr in cls.keywords.items():
            config_data = self.get_config_update(name, expr)
            if config_data:
                has_config_kwargs = True
                config.update(config_data)

        for stmt in cls.defs.body:
            if not isinstance(stmt, (AssignmentStmt, ClassDef)):
                continue

            if isinstance(stmt, AssignmentStmt):
                lhs = stmt.lvalues[0]
                if not isinstance(lhs, NameExpr) or lhs.name != 'model_config' or not isinstance(stmt.rvalue, CallExpr):
                    continue
                for arg_name, arg in zip(stmt.rvalue.arg_names, stmt.rvalue.args):
                    if arg_name is None:
                        continue
                    config.update(self.get_config_update(arg_name, arg))

            if isinstance(stmt, ClassDef):
                if stmt.name != 'Config':  # 'deprecated' Config-class
                    continue
                for substmt in stmt.defs.body:
                    if not isinstance(substmt, AssignmentStmt):
                        continue
                    lhs = substmt.lvalues[0]
                    if not isinstance(lhs, NameExpr):
                        continue
                    config.update(self.get_config_update(lhs.name, substmt.rvalue))

            if has_config_kwargs:
                ctx.api.fail(
                    'Specifying config in two places is ambiguous, use either Config attribute or class kwargs',
                    cls,
                )
                break

            has_config_from_namespace = True

        if has_config_kwargs or has_config_from_namespace:
            if (
                config.has_alias_generator
                and not config.populate_by_name
                and self.plugin_config.warn_required_dynamic_aliases
            ):
                error_required_dynamic_aliases(ctx.api, stmt)
        for info in cls.info.mro[1:]:  # 0 is the current class
            if METADATA_KEY not in info.metadata:
                continue

            # Each class depends on the set of fields in its ancestors
            ctx.api.add_plugin_dependency(make_wildcard_trigger(get_fullname(info)))
            for name, value in info.metadata[METADATA_KEY]['config'].items():
                config.setdefault(name, value)
        return config

    def collect_fields(self, model_config: ModelConfigData) -> list[PydanticModelField]:
        """
        Collects the fields for the model, accounting for parent classes
        """
        # First, collect fields belonging to the current class.
        ctx = self._ctx
        cls = self._ctx.cls
        fields: list[PydanticModelField] = []
        known_fields: set[str] = set()
        for stmt in cls.defs.body:
            maybe_field = self.collect_field_from_stmt(stmt, model_config)
            if maybe_field is not None:
                fields.append(maybe_field)
                known_fields.add(maybe_field.name)

        all_fields = fields.copy()
        for info in cls.info.mro[1:]:  # 0 is the current class, -2 is BaseModel, -1 is object
            if METADATA_KEY not in info.metadata:
                continue

            superclass_fields = []
            # Each class depends on the set of fields in its ancestors
            ctx.api.add_plugin_dependency(make_wildcard_trigger(get_fullname(info)))

            for name, data in info.metadata[METADATA_KEY]['fields'].items():
                if name not in known_fields:
                    field = PydanticModelField.deserialize(info, data)
                    known_fields.add(name)
                    superclass_fields.append(field)
                else:
                    (field,) = (a for a in all_fields if a.name == name)
                    all_fields.remove(field)
                    superclass_fields.append(field)
            all_fields = superclass_fields + all_fields
        return all_fields

    def collect_field_from_stmt(self, stmt: Statement, model_config: ModelConfigData) -> PydanticModelField | None:
        ctx = self._ctx
        cls = self._ctx.cls
        if not isinstance(stmt, AssignmentStmt):
            return None

        lhs = stmt.lvalues[0]
        if not isinstance(lhs, NameExpr) or lhs.name.startswith('_') or lhs.name == 'model_config':
            return None

        if not stmt.new_syntax:
            if (
                isinstance(stmt.rvalue, CallExpr)
                and isinstance(stmt.rvalue.callee, CallExpr)
                and isinstance(stmt.rvalue.callee.callee, NameExpr)
                and stmt.rvalue.callee.callee.fullname in DECORATOR_FULLNAMES
            ):
                # This is a (possibly-reused) validator or serializer, not a field
                # In particular, it looks something like: my_validator = validator('my_field')(f)
                # Eventually, we may want to attempt to respect model_config['ignored_types']
                return None

            # The assignment does not have an annotation, and it's not anything else we recognize
            error_untyped_fields(ctx.api, stmt)
            return None

        sym = cls.info.names.get(lhs.name)
        if sym is None:  # pragma: no cover
            # This is likely due to a star import (see the dataclasses plugin for a more detailed explanation)
            # This is the same logic used in the dataclasses plugin
            return None

        node = sym.node
        if isinstance(node, PlaceholderNode):  # pragma: no cover
            # See the PlaceholderNode docstring for more detail about how this can occur
            # Basically, it is an edge case when dealing with complex import logic
            # This is the same logic used in the dataclasses plugin
            return None
        if not isinstance(node, Var):  # pragma: no cover
            # Don't know if this edge case still happens with the `is_valid_field` check above
            # but better safe than sorry
            return None

        # x: ClassVar[int] is ignored by dataclasses.
        if node.is_classvar:
            return None

        is_required = self.get_is_required(cls, stmt, lhs)
        alias, has_dynamic_alias = self.get_alias_info(stmt)
        if has_dynamic_alias and not model_config.populate_by_name and self.plugin_config.warn_required_dynamic_aliases:
            error_required_dynamic_aliases(ctx.api, stmt)
        return PydanticModelField(
            name=lhs.name,
            is_required=is_required,
            alias=alias,
            has_dynamic_alias=has_dynamic_alias,
            line=stmt.line,
            column=stmt.column,
        )

    def add_initializer(self, fields: list[PydanticModelField], config: ModelConfigData) -> None:
        """
        Adds a fields-aware `__init__` method to the class.

        The added `__init__` will be annotated with types vs. all `Any` depending on the plugin settings.
        """
        ctx = self._ctx
        typed = self.plugin_config.init_typed
        use_alias = config.populate_by_name is not True
        force_all_optional = bool(config.has_alias_generator and not config.populate_by_name)
        init_arguments = self.get_field_arguments(
            fields, typed=typed, force_all_optional=force_all_optional, use_alias=use_alias
        )
        if not self.should_init_forbid_extra(fields, config):
            var = Var('kwargs')
            init_arguments.append(Argument(var, AnyType(TypeOfAny.explicit), None, ARG_STAR2))

        if '__init__' not in ctx.cls.info.names:
            add_method(ctx, '__init__', init_arguments, NoneType())

    def add_model_construct_method(self, fields: list[PydanticModelField]) -> None:
        """
        Adds a fully typed `model_construct` classmethod to the class.

        Similar to the fields-aware __init__ method, but always uses the field names (not aliases),
        and does not treat settings fields as optional.
        """
        ctx = self._ctx
        set_str = ctx.api.named_type(f'{BUILTINS_NAME}.set', [ctx.api.named_type(f'{BUILTINS_NAME}.str')])
        optional_set_str = UnionType([set_str, NoneType()])
        fields_set_argument = Argument(Var('_fields_set', optional_set_str), optional_set_str, None, ARG_OPT)
        construct_arguments = self.get_field_arguments(fields, typed=True, force_all_optional=False, use_alias=False)
        construct_arguments = [fields_set_argument] + construct_arguments

        obj_type = ctx.api.named_type(f'{BUILTINS_NAME}.object')
        self_tvar_name = '_PydanticBaseModel'  # Make sure it does not conflict with other names in the class
        tvar_fullname = ctx.cls.fullname + '.' + self_tvar_name
        # requires mypy>0.910
        self_type = TypeVarDef(self_tvar_name, tvar_fullname, -1, [], obj_type)
        self_tvar_expr = TypeVarExpr(self_tvar_name, tvar_fullname, [], obj_type)
        ctx.cls.info.names[self_tvar_name] = SymbolTableNode(MDEF, self_tvar_expr)

        add_method(
            ctx,
            'model_construct',
            construct_arguments,
            return_type=self_type,
            self_type=self_type,
            tvar_def=self_type,
            is_classmethod=True,
        )

    def set_frozen(self, fields: list[PydanticModelField], frozen: bool) -> None:
        """
        Marks all fields as properties so that attempts to set them trigger mypy errors.

        This is the same approach used by the attrs and dataclasses plugins.
        """
        ctx = self._ctx
        info = ctx.cls.info
        for field in fields:
            sym_node = info.names.get(field.name)
            if sym_node is not None:
                var = sym_node.node
                if isinstance(var, Var):
                    var.is_property = frozen
                elif isinstance(var, PlaceholderNode) and not ctx.api.final_iteration:
                    # See https://github.com/pydantic/pydantic/issues/5191 to hit this branch for test coverage
                    ctx.api.defer()
                else:  # pragma: no cover
                    # I don't know whether it's possible to hit this branch, but I've added it for safety
                    try:
                        var_str = str(var)
                    except TypeError:
                        # This happens for PlaceholderNode; perhaps it will happen for other types in the future..
                        var_str = repr(var)
                    detail = f'sym_node.node: {var_str} (of type {var.__class__})'
                    error_unexpected_behavior(detail, ctx.api, ctx.cls)
            else:
                var = field.to_var(info, use_alias=False)
                var.info = info
                var.is_property = frozen
                var._fullname = get_fullname(info) + '.' + get_name(var)
                info.names[get_name(var)] = SymbolTableNode(MDEF, var)

    def get_config_update(self, name: str, arg: Expression) -> ModelConfigData | None:
        """
        Determines the config update due to a single kwarg in the ConfigDict definition.

        Warns if a tracked config attribute is set to a value the plugin doesn't know how to interpret (e.g., an int)
        """
        if name not in self.tracked_config_fields:
            return None
        if name == 'extra':
            if isinstance(arg, StrExpr):
                forbid_extra = arg.value == 'forbid'
            elif isinstance(arg, MemberExpr):
                forbid_extra = arg.name == 'forbid'
            else:
                error_invalid_config_value(name, self._ctx.api, arg)
                return None
            return ModelConfigData(forbid_extra=forbid_extra)
        if name == 'alias_generator':
            has_alias_generator = True
            if isinstance(arg, NameExpr) and arg.fullname == 'builtins.None':
                has_alias_generator = False
            return ModelConfigData(has_alias_generator=has_alias_generator)
        if isinstance(arg, NameExpr) and arg.fullname in ('builtins.True', 'builtins.False'):
            return ModelConfigData(**{name: arg.fullname == 'builtins.True'})
        error_invalid_config_value(name, self._ctx.api, arg)
        return None

    @staticmethod
    def get_is_required(cls: ClassDef, stmt: AssignmentStmt, lhs: NameExpr) -> bool:
        """
        Returns a boolean indicating whether the field defined in `stmt` is a required field.
        """
        expr = stmt.rvalue
        if isinstance(expr, TempNode):
            # TempNode means annotation-only, so only non-required if Optional
            value_type = get_proper_type(cls.info[lhs.name].type)
            if isinstance(value_type, UnionType) and any(isinstance(item, NoneType) for item in value_type.items):
                # Annotated as Optional, or otherwise having NoneType in the union
                return False
            return True
        if isinstance(expr, CallExpr) and isinstance(expr.callee, RefExpr) and expr.callee.fullname == FIELD_FULLNAME:
            # The "default value" is a call to `Field`; at this point, the field is
            # only required if default is Ellipsis (i.e., `field_name: Annotation = Field(...)`) or if default_factory
            # is specified.
            for arg, name in zip(expr.args, expr.arg_names):
                # If name is None, then this arg is the default because it is the only positional argument.
                if name is None or name == 'default':
                    return arg.__class__ is EllipsisExpr
                if name == 'default_factory':
                    return False
            return True
        # Only required if the "default value" is Ellipsis (i.e., `field_name: Annotation = ...`)
        return isinstance(expr, EllipsisExpr)

    @staticmethod
    def get_alias_info(stmt: AssignmentStmt) -> tuple[str | None, bool]:
        """
        Returns a pair (alias, has_dynamic_alias), extracted from the declaration of the field defined in `stmt`.

        `has_dynamic_alias` is True if and only if an alias is provided, but not as a string literal.
        If `has_dynamic_alias` is True, `alias` will be None.
        """
        expr = stmt.rvalue
        if isinstance(expr, TempNode):
            # TempNode means annotation-only
            return None, False

        if not (
            isinstance(expr, CallExpr) and isinstance(expr.callee, RefExpr) and expr.callee.fullname == FIELD_FULLNAME
        ):
            # Assigned value is not a call to pydantic.fields.Field
            return None, False

        for i, arg_name in enumerate(expr.arg_names):
            if arg_name != 'alias':
                continue
            arg = expr.args[i]
            if isinstance(arg, StrExpr):
                return arg.value, False
            else:
                return None, True
        return None, False

    def get_field_arguments(
        self, fields: list[PydanticModelField], typed: bool, force_all_optional: bool, use_alias: bool
    ) -> list[Argument]:
        """
        Helper function used during the construction of the `__init__` and `model_construct` method signatures.

        Returns a list of mypy Argument instances for use in the generated signatures.
        """
        info = self._ctx.cls.info
        arguments = [
            field.to_argument(info, typed=typed, force_optional=force_all_optional, use_alias=use_alias)
            for field in fields
            if not (use_alias and field.has_dynamic_alias)
        ]
        return arguments

    def should_init_forbid_extra(self, fields: list[PydanticModelField], config: ModelConfigData) -> bool:
        """
        Indicates whether the generated `__init__` should get a `**kwargs` at the end of its signature

        We disallow arbitrary kwargs if the extra config setting is "forbid", or if the plugin config says to,
        *unless* a required dynamic alias is present (since then we can't determine a valid signature).
        """
        if not config.populate_by_name:
            if self.is_dynamic_alias_present(fields, bool(config.has_alias_generator)):
                return False
        if config.forbid_extra:
            return True
        return self.plugin_config.init_forbid_extra

    @staticmethod
    def is_dynamic_alias_present(fields: list[PydanticModelField], has_alias_generator: bool) -> bool:
        """
        Returns whether any fields on the model have a "dynamic alias", i.e., an alias that cannot be
        determined during static analysis.
        """
        for field in fields:
            if field.has_dynamic_alias:
                return True
        if has_alias_generator:
            for field in fields:
                if field.alias is None:
                    return True
        return False


class PydanticModelField:
    def __init__(
        self, name: str, is_required: bool, alias: str | None, has_dynamic_alias: bool, line: int, column: int
    ):
        self.name = name
        self.is_required = is_required
        self.alias = alias
        self.has_dynamic_alias = has_dynamic_alias
        self.line = line
        self.column = column

    def to_var(self, info: TypeInfo, use_alias: bool) -> Var:
        name = self.name
        if use_alias and self.alias is not None:
            name = self.alias
        return Var(name, info[self.name].type)

    def to_argument(self, info: TypeInfo, typed: bool, force_optional: bool, use_alias: bool) -> Argument:
        if typed and info[self.name].type is not None:
            type_annotation = info[self.name].type
        else:
            type_annotation = AnyType(TypeOfAny.explicit)
        return Argument(
            variable=self.to_var(info, use_alias),
            type_annotation=type_annotation,
            initializer=None,
            kind=ARG_NAMED_OPT if force_optional or not self.is_required else ARG_NAMED,
        )

    def serialize(self) -> JsonDict:
        return self.__dict__

    @classmethod
    def deserialize(cls, info: TypeInfo, data: JsonDict) -> PydanticModelField:
        return cls(**data)


class ModelConfigData:
    def __init__(
        self,
        forbid_extra: bool | None = None,
        frozen: bool | None = None,
        from_attributes: bool | None = None,
        populate_by_name: bool | None = None,
        has_alias_generator: bool | None = None,
    ):
        self.forbid_extra = forbid_extra
        self.frozen = frozen
        self.from_attributes = from_attributes
        self.populate_by_name = populate_by_name
        self.has_alias_generator = has_alias_generator

    def set_values_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def update(self, config: ModelConfigData | None) -> None:
        if config is None:
            return
        for k, v in config.set_values_dict().items():
            setattr(self, k, v)

    def setdefault(self, key: str, value: Any) -> None:
        if getattr(self, key) is None:
            setattr(self, key, value)


ERROR_ORM = ErrorCode('pydantic-orm', 'Invalid from_attributes call', 'Pydantic')
ERROR_CONFIG = ErrorCode('pydantic-config', 'Invalid config value', 'Pydantic')
ERROR_ALIAS = ErrorCode('pydantic-alias', 'Dynamic alias disallowed', 'Pydantic')
ERROR_UNEXPECTED = ErrorCode('pydantic-unexpected', 'Unexpected behavior', 'Pydantic')
ERROR_UNTYPED = ErrorCode('pydantic-field', 'Untyped field disallowed', 'Pydantic')
ERROR_FIELD_DEFAULTS = ErrorCode('pydantic-field', 'Invalid Field defaults', 'Pydantic')


def error_from_attributes(model_name: str, api: CheckerPluginInterface, context: Context) -> None:
    api.fail(f'"{model_name}" does not have from_attributes=True', context, code=ERROR_ORM)


def error_invalid_config_value(name: str, api: SemanticAnalyzerPluginInterface, context: Context) -> None:
    api.fail(f'Invalid value for "Config.{name}"', context, code=ERROR_CONFIG)


def error_required_dynamic_aliases(api: SemanticAnalyzerPluginInterface, context: Context) -> None:
    api.fail('Required dynamic aliases disallowed', context, code=ERROR_ALIAS)


def error_unexpected_behavior(
    detail: str, api: CheckerPluginInterface | SemanticAnalyzerPluginInterface, context: Context
) -> None:  # pragma: no cover
    # Can't think of a good way to test this, but I confirmed it renders as desired by adding to a non-error path
    link = 'https://github.com/pydantic/pydantic/issues/new/choose'
    full_message = f'The pydantic mypy plugin ran into unexpected behavior: {detail}\n'
    full_message += f'Please consider reporting this bug at {link} so we can try to fix it!'
    api.fail(full_message, context, code=ERROR_UNEXPECTED)


def error_untyped_fields(api: SemanticAnalyzerPluginInterface, context: Context) -> None:
    api.fail('Untyped fields disallowed', context, code=ERROR_UNTYPED)


def error_default_and_default_factory_specified(api: CheckerPluginInterface, context: Context) -> None:
    api.fail('Field default and default_factory cannot be specified together', context, code=ERROR_FIELD_DEFAULTS)


def add_method(
    ctx: ClassDefContext,
    name: str,
    args: list[Argument],
    return_type: Type,
    self_type: Type | None = None,
    tvar_def: TypeVarDef | None = None,
    is_classmethod: bool = False,
    is_new: bool = False,
    # is_staticmethod: bool = False,
) -> None:
    """
    Adds a new method to a class.

    This can be dropped if/when https://github.com/python/mypy/issues/7301 is merged
    """
    info = ctx.cls.info

    # First remove any previously generated methods with the same name
    # to avoid clashes and problems in the semantic analyzer.
    if name in info.names:
        sym = info.names[name]
        if sym.plugin_generated and isinstance(sym.node, FuncDef):
            ctx.cls.defs.body.remove(sym.node)  # pragma: no cover

    self_type = self_type or fill_typevars(info)
    if is_classmethod or is_new:
        first = [Argument(Var('_cls'), TypeType.make_normalized(self_type), None, ARG_POS)]
    # elif is_staticmethod:
    #     first = []
    else:
        self_type = self_type or fill_typevars(info)
        first = [Argument(Var('__pydantic_self__'), self_type, None, ARG_POS)]
    args = first + args
    arg_types, arg_names, arg_kinds = [], [], []
    for arg in args:
        assert arg.type_annotation, 'All arguments must be fully typed.'
        arg_types.append(arg.type_annotation)
        arg_names.append(get_name(arg.variable))
        arg_kinds.append(arg.kind)

    function_type = ctx.api.named_type(f'{BUILTINS_NAME}.function')
    signature = CallableType(arg_types, arg_kinds, arg_names, return_type, function_type)
    if tvar_def:
        signature.variables = [tvar_def]

    func = FuncDef(name, args, Block([PassStmt()]))
    func.info = info
    func.type = set_callable_name(signature, func)
    func.is_class = is_classmethod
    # func.is_static = is_staticmethod
    func._fullname = get_fullname(info) + '.' + name
    func.line = info.line

    # NOTE: we would like the plugin generated node to dominate, but we still
    # need to keep any existing definitions so they get semantically analyzed.
    if name in info.names:
        # Get a nice unique name instead.
        r_name = get_unique_redefinition_name(name, info.names)
        info.names[r_name] = info.names[name]

    if is_classmethod:  # or is_staticmethod:
        func.is_decorated = True
        v = Var(name, func.type)
        v.info = info
        v._fullname = func._fullname
        # if is_classmethod:
        v.is_classmethod = True
        dec = Decorator(func, [NameExpr('classmethod')], v)
        # else:
        #     v.is_staticmethod = True
        #     dec = Decorator(func, [NameExpr('staticmethod')], v)

        dec.line = info.line
        sym = SymbolTableNode(MDEF, dec)
    else:
        sym = SymbolTableNode(MDEF, func)
    sym.plugin_generated = True

    info.names[name] = sym
    info.defn.defs.body.append(func)


def get_fullname(x: FuncBase | SymbolNode) -> str:
    """
    Used for compatibility with mypy 0.740; can be dropped once support for 0.740 is dropped.
    """
    fn = x.fullname
    if callable(fn):  # pragma: no cover
        return fn()
    return fn


def get_name(x: FuncBase | SymbolNode) -> str:
    """
    Used for compatibility with mypy 0.740; can be dropped once support for 0.740 is dropped.
    """
    fn = x.name
    if callable(fn):  # pragma: no cover
        return fn()
    return fn


def parse_toml(config_file: str) -> dict[str, Any] | None:
    if not config_file.endswith('.toml'):
        return None

    if sys.version_info >= (3, 11):
        import tomllib as toml_
    else:
        try:
            import tomli as toml_
        except ImportError:  # pragma: no cover
            import warnings

            warnings.warn('No TOML parser installed, cannot read configuration from `pyproject.toml`.')
            return None

    with open(config_file, 'rb') as rf:
        return toml_.load(rf)
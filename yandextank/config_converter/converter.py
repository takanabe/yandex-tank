import ConfigParser
import re
import logging
import pkg_resources

from yandextank.common.util import recursive_dict_update
from yandextank.validator.validator import load_plugin_schema, load_yaml_schema

logger = logging.getLogger(__name__)
CORE_SCHEMA = load_yaml_schema(pkg_resources.resource_filename('yandextank.core', 'config/schema.yaml'))['core']['schema']


def old_plugin_mapper(package):
    MAP = {'Overload': 'DataUploader'}
    return MAP.get(package, package)


def parse_package_name(package_path):
    if package_path.startswith("Tank/Plugins/"):
        package = package_path.split('/')[-1].split('.')[0]
    else:
        package = package_path.split('.')[-1].split()[0]
    return old_plugin_mapper(package)


SECTIONS_PATTERNS = {
    'tank': 'core|tank',
    'Aggregator': 'aggregator',
    'Android': 'android',
    'Appium': 'appium',
    'Autostop': 'autostop',
    'BatteryHistorian': 'battery_historian',
    'Bfg': 'bfg|ultimate_gun|http_gun|custom_gun|scenario_gun',
    'Phantom': 'phantom(-.*)?',
    'DataUploader': 'meta|overload',
    'Telegraf': 'telegraf|monitoring',
    'JMeter': 'jmeter',
    'ResourceCheck': 'rcheck',
    'ShellExec': 'shellexec',
    'Console': 'console',
    'TipsAndTricks': 'tips',
    'RCAssert': 'rcassert',
    'JsonReport': 'json_report|jsonreport'
}


class UnrecognizedSection(Exception):
    pass


def guess_plugin(section):
    for plugin, section_name_pattern in SECTIONS_PATTERNS.items():
        if re.match(section_name_pattern, section):
            return plugin
    else:
        raise UnrecognizedSection('Section {} did not match any plugin'.format(section))


def convert_rps_schedule(key, value):
    return {'load_profile': {
        'load_type': 'rps',
        'schedule': value
    }}


def convert_instances_schedule(key, value):
    return {'load_profile': {
        'load_type': 'instances',
        'schedule': value
    }}


def convert_stpd_schedule(key, value):
    return {'load_profile': {
        'load_type': 'stpd_file',
        'schedule': value
    }}


def to_bool(value):
    try:
        return bool(int(value))
    except ValueError:
        return True if 'true' == value.lower() else False


def is_option_deprecated(plugin, option_name):
    DEPRECATED = {
        'Aggregator': [
            'time_periods',
            'precise_cumulative'
        ]
    }
    if option_name in DEPRECATED.get(plugin, []):
        logger.warning('Deprecated option {} in plugin {}, omitting'.format(option_name, plugin))
        return True
    else:
        return False


def without_deprecated(plugin, options):
    """
    :type options: list of tuple
    """
    return filter(lambda option: not is_option_deprecated(plugin, option[0]), options)


def old_section_name_mapper(name):
    MAP = {
        'monitoring': 'telegraf',
        'meta': 'uploader'
    }
    return MAP.get(name, name)


class Package(object):
    def __init__(self, package_path):
        if package_path.startswith("Tank/Plugins/"):
            self.package = package_path.split('.')[0].replace('Tank/Plugins/', 'yandextank.plugins.')
        else:
            self.package = package_path
        self.plugin_name = old_plugin_mapper(self.package.split('.')[-1])


class UnknownOption(Exception):
    pass


class Option(object):
    SPECIAL_CONVERTERS = {
        'Phantom': {
            'rps_schedule': convert_rps_schedule,
            'instances_schedule': convert_instances_schedule,
            'stpd_file': convert_stpd_schedule,
        },
        'Bfg': {
            'rps_schedule': convert_rps_schedule,
            'instances_schedule': convert_instances_schedule,
        },
        'JMeter': {
            'exclude_markers': lambda key, value: {key: value.strip().split(' ')}
        }
    }
    CONVERTERS_FOR_UNKNOWN = {
        'DataUploader': lambda k, v: {'meta': {k: v}},
        'JMeter': lambda k, v: {'variables': {k: v}}
    }

    def __init__(self, plugin_name, key, value, schema=None):
        self.plugin = plugin_name
        self.name = key
        self.value = value
        self.schema = schema
        self.dummy_converter = lambda k, v: {k: v}
        self._converted = None
        self._converter = None
        self._as_tuple = None

    @property
    def converted(self):
        """
        :rtype: {str: object}
        """
        if self._converted is None:
            self._converted = self.converter(self.name, self.value)
        return self._converted

    @property
    def as_tuple(self):
        """
        :rtype: (str, object)
        """
        if self._as_tuple is None:
            self._as_tuple = self.converted.items()[0]
        return self._as_tuple

    @property
    def converter(self):
        """
        :rtype: callable
        """
        if self._converter is None:
            try:
                return self.SPECIAL_CONVERTERS[self.plugin][self.name]
            except KeyError:
                try:
                    return self._get_scheme_converter()
                except UnknownOption:
                    return self.CONVERTERS_FOR_UNKNOWN.get(self.plugin, self.dummy_converter)

    def _get_scheme_converter(self):
        type_casters = {
            'boolean': lambda k, v: {k: to_bool(v)},
            'integer': lambda k, v: {k: int(v)},
            'list': lambda k, v: {k: [_.strip() for _ in v.strip().split('\n')]},
            'float': lambda k, v: {k: float(v)}
        }
        modulepath_path = {
            'tank': 'yandextank.core'
        }

        def default_path(plugin):
            'yandextank.plugins.{}'.format(plugin)

        schema = self.schema if self.schema else \
            load_plugin_schema(modulepath_path.get(self.plugin, default_path(self.plugin)))

        if schema.get(self.name) is None:
            logger.warning('Unknown option {}:{}'.format(self.plugin, self.name))
            raise UnknownOption

        _type = schema[self.name].get('type', None)
        if _type is None:
            logger.warning('Option {}:{}: no type specified in schema'.format(self.plugin, self.name))
            return self.dummy_converter

        return type_casters.get(_type, self.dummy_converter)


class Section(object):
    def __init__(self, name, plugin, options, enabled=None):
        self.init_name = name
        self.name = old_section_name_mapper(name)
        self.plugin = plugin
        self._schema = None
        self.options = [Option(plugin, *option, schema=self.schema) for option in without_deprecated(plugin, options)]
        self.enabled = enabled
        self._merged_options = None

    @property
    def schema(self):
        if self._schema is None:
            self._schema = load_plugin_schema('yandextank.plugins.' + self.plugin)
        return self._schema

    def get_cfg_dict(self, with_meta=True):
        options_dict = self.merged_options
        if with_meta:
            if self.plugin:
                options_dict.update({'package': 'yandextank.plugins.{}'.format(self.plugin)})
            if self.enabled is not None:
                options_dict.update({'enabled': self.enabled})
        return options_dict

    @property
    def merged_options(self):
        if self._merged_options is None:
            self._merged_options = reduce(lambda acc, upd: recursive_dict_update(acc, upd),
                                          [opt.converted for opt in self.options],
                                          {})
        return self._merged_options

    @classmethod
    def from_multiple(cls, sections, parent_name=None, child_name=None, is_list=True):
        """
        :type parent_name: str
        :type sections: list of Section
        """
        if len(sections) == 1:
            return sections[0]
        if parent_name:
            master_section = filter(lambda section: section.name == parent_name, sections)[0]
            rest = filter(lambda section: section.name != parent_name, sections)
        else:
            master_section = sections[0]
            parent_name = master_section.name
            rest = sections[1:]
        child = {'multi': [section.get_cfg_dict(with_meta=False) for section in rest]} if is_list \
            else {child_name: rest[0].get_cfg_dict(with_meta=False)}
        master_section.merged_options.update(child)
        return master_section


def without_defaults(cfg_ini, section):
    """

    :rtype: (str, str)
    :type cfg_ini: ConfigParser.ConfigParser
    """
    defaults = cfg_ini.defaults()
    options = cfg_ini.items(section) if cfg_ini.has_section(section) else []
    return [(key, value) for key, value in options if key not in defaults.keys()]


PLUGIN_PREFIX = 'plugin_'
CORE_SECTION = 'tank'


def parse_sections(cfg_ini):
    """
    :type cfg_ini: ConfigParser.ConfigParser
    """
    return [Section(section,
                    guess_plugin(section),
                    without_defaults(cfg_ini, section))
            for section in cfg_ini.sections()
            if section != CORE_SECTION]


class PluginInstance(object):
    def __init__(self, name, package_and_section):
        self.name = name
        self.enabled = len(package_and_section) > 0
        try:
            package_path, self.section_name = package_and_section.split()
            self.package = Package(package_path)
        except ValueError:
            self.package = Package(package_and_section)
            self.section_name = self._guess_section_name()
        self.plugin_name = self.package.plugin_name

    def _guess_section_name(self):
        package_map = {
            'Aggregator': 'aggregator',
            'Autostop': 'autostop',
            'BatteryHistorian': 'battery_historian',
            'Bfg': 'bfg',
            'Console': 'console',
            'DataUploader': 'meta',
            'JMeter': 'jmeter',
            'JsonReport': 'json_report',
            'Maven': 'maven',
            'Monitoring': 'monitoring',
            'Pandora': 'pandora',
            'Phantom': 'phantom',
            'RCAssert': 'rcassert',
            'ResourceCheck': 'rcheck',
            'ShellExec': 'shellexec',
            'SvgReport': 'svgreport',
            'Telegraf': 'telegraf',
            'TipsAndTricks': 'tips'
        }
        name_map = {
            'aggregate': 'aggregator',
            'datauploader': 'uploader',
            'lunapark': 'uploader',
            'overload': 'overload',
            'uploader': 'uploader',
            'jsonreport': 'json_report'
        }
        return name_map.get(self.name, package_map.get(self.package.plugin_name, self.name))


def enable_sections(sections, core_opts):
    """

    :type sections: list of Section
    """
    plugin_instances = [PluginInstance(key.split('_')[1], value) for key, value in core_opts if
                        key.startswith(PLUGIN_PREFIX)]
    enabled_instances = {instance.section_name: instance for instance in plugin_instances if instance.enabled}
    disabled_instances = {instance.section_name: instance for instance in plugin_instances if not instance.enabled}

    for section in sections:
        if section.name in enabled_instances.keys():
            section.enabled = True
            enabled_instances.pop(section.name)
        elif section.name in disabled_instances.keys():
            section.enabled = False
            disabled_instances.pop(section.name)
    # add leftovers
    for plugin_instance in [i for i in plugin_instances if
                            i.section_name in enabled_instances.keys() + disabled_instances.keys()]:
        sections.append(Section(plugin_instance.section_name, plugin_instance.plugin_name, [], plugin_instance.enabled))
    return sections


def partition(l, predicate):
    return reduce(lambda x, y: (x[0] + [y], x[1]) if predicate(y) else (x[0], x[1] + [y]), l, ([], []))


def combine_sections(sections):
    """
    :type sections: list of Section
    :rtype: list of Section
    """
    PLUGINS_TO_COMBINE = {
        'Phantom': ('phantom', 'multi', True),
        'Bfg': ('bfg', 'gun_config', False)
    }
    plugins = {}
    ready_sections = []
    for section in sections:
        if section.plugin in PLUGINS_TO_COMBINE.keys():
            try:
                plugins[section.plugin].append(section)
            except KeyError:
                plugins[section.plugin] = [section]
        else:
            ready_sections.append(section)

    for plugin_name, _sections in plugins.items():
        if isinstance(_sections, list):
            parent_name, child_name, is_list = PLUGINS_TO_COMBINE[plugin_name]
            ready_sections.append(Section.from_multiple(_sections, parent_name, child_name, is_list))
    return ready_sections


def core_options(cfg_ini):
    return cfg_ini.items(CORE_SECTION) if cfg_ini.has_section(CORE_SECTION) else []


def convert_ini(ini_file):
    cfg_ini = ConfigParser.ConfigParser()
    cfg_ini.read(ini_file)
    ready_sections = enable_sections(combine_sections(parse_sections(cfg_ini)), core_options(cfg_ini))

    plugins_cfg_dict = {section.name: section.get_cfg_dict() for section in ready_sections}

    plugins_cfg_dict.update({
        'core': dict([Option('core', key, value, CORE_SCHEMA).as_tuple
                      for key, value in without_defaults(cfg_ini, CORE_SECTION)
                      if not key.startswith(PLUGIN_PREFIX)])
    })
    return plugins_cfg_dict


def convert_single_option(key, value):
    """

    :type value: str
    :type key: str
    :rtype: {str: obj}
    """
    section_name, option_name = key.strip().split('.')
    if section_name != CORE_SECTION:
        section = Section(section_name,
                          guess_plugin(section_name),
                          [(option_name, value)])
        return {section.name: section.get_cfg_dict()}
    else:
        if option_name.startswith(PLUGIN_PREFIX):
            return {section.name: section.get_cfg_dict() for section in enable_sections([], [(option_name, value)])}
        else:
            return {'core': Option('core', option_name, value, CORE_SCHEMA).converted}

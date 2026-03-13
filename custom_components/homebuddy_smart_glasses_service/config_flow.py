from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.conversation import HOME_ASSISTANT_AGENT, async_get_agent_info
from homeassistant.components.conversation.agent_manager import get_agent_manager

from .const import CONF_ADDON_HOST, CONF_ADDON_PORT, CONF_AGENT_ID, DEFAULT_ADDON_HOST, DEFAULT_ADDON_PORT, DOMAIN


async def _agent_choices(hass) -> dict[str, str]:
    choices: dict[str, str] = {}

    default_info = async_get_agent_info(hass, HOME_ASSISTANT_AGENT)
    choices[HOME_ASSISTANT_AGENT] = default_info.name if default_info else "Home Assistant"

    for info in get_agent_manager(hass).async_get_agent_info():
        choices[info.id] = info.name

    return choices


def _schema(addon_host: str, addon_port: int, agent_id: str, agent_choices: dict[str, str]) -> vol.Schema:
    if agent_id not in agent_choices:
        agent_id = next(iter(agent_choices), HOME_ASSISTANT_AGENT)
    return vol.Schema(
        {
            vol.Required(CONF_ADDON_HOST, default=addon_host): str,
            vol.Required(CONF_ADDON_PORT, default=addon_port): int,
            vol.Required(CONF_AGENT_ID, default=agent_id): vol.In(agent_choices),
        }
    )


class HomeBuddySmartGlassesServiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="HomeBuddy Smart Glasses Service", data=user_input)

        agent_choices = await _agent_choices(self.hass)
        default_agent_id = next(iter(agent_choices), HOME_ASSISTANT_AGENT)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(DEFAULT_ADDON_HOST, DEFAULT_ADDON_PORT, default_agent_id, agent_choices),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return HomeBuddySmartGlassesServiceOptionsFlow(config_entry)


class HomeBuddySmartGlassesServiceOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        agent_choices = await _agent_choices(self.hass)
        default_agent_id = self.config_entry.options.get(
            CONF_AGENT_ID,
            self.config_entry.data.get(CONF_AGENT_ID, next(iter(agent_choices), HOME_ASSISTANT_AGENT)),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                self.config_entry.options.get(CONF_ADDON_HOST, self.config_entry.data.get(CONF_ADDON_HOST, DEFAULT_ADDON_HOST)),
                self.config_entry.options.get(CONF_ADDON_PORT, self.config_entry.data.get(CONF_ADDON_PORT, DEFAULT_ADDON_PORT)),
                default_agent_id,
                agent_choices,
            ),
        )

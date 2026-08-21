import { defineChannelBridgeAddon } from "@angee/messaging";

import { ConnectDiscordChannelAction } from "./ConnectDiscordChannelAction";
import { enMessagingDiscordMessages } from "./i18n";

const messagingIntegrateDiscord = defineChannelBridgeAddon({
  id: "messaging-integrate-discord",
  key: "discord",
  sequence: 25,
  connectAction: <ConnectDiscordChannelAction />,
  i18n: enMessagingDiscordMessages,
});

export default messagingIntegrateDiscord;

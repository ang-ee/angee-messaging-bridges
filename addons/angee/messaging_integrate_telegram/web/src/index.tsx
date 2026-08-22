import { defineChannelBridgeAddon } from "@angee/messaging";

import { ConnectTelegramChannelAction } from "./ConnectTelegramChannelAction";
import { enMessagingTelegramMessages } from "./i18n";

const messagingIntegrateTelegram = defineChannelBridgeAddon({
  id: "messaging-integrate-telegram",
  key: "telegram",
  sequence: 21,
  connectAction: <ConnectTelegramChannelAction />,
  i18n: { messaging: enMessagingTelegramMessages },
  instructionKey: "channel.telegram.scan",
});

export default messagingIntegrateTelegram;

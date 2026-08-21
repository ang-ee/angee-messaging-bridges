import { defineChannelBridgeAddon } from "@angee/messaging";

import { ConnectWhatsappChannelAction } from "./ConnectWhatsappChannelAction";
import { enMessagingWhatsappMessages } from "./i18n";

const messagingIntegrateWhatsapp = defineChannelBridgeAddon({
  id: "messaging-integrate-whatsapp",
  key: "whatsapp",
  sequence: 20,
  connectAction: <ConnectWhatsappChannelAction />,
  i18n: enMessagingWhatsappMessages,
  instructionKey: "channel.whatsapp.scan",
});

export default messagingIntegrateWhatsapp;

import { defineChannelBridgeAddon } from "@angee/messaging";

import { ConnectSignalChannelAction } from "./ConnectSignalChannelAction";
import { enMessagingSignalMessages } from "./i18n";

const messagingIntegrateSignal = defineChannelBridgeAddon({
  id: "messaging-integrate-signal",
  key: "signal",
  sequence: 22,
  connectAction: <ConnectSignalChannelAction />,
  i18n: enMessagingSignalMessages,
  instructionKey: "channel.signal.scan",
});

export default messagingIntegrateSignal;

import { defineChannelBridgeAddon } from "@angee/messaging";

import { ConnectMatrixChannelAction } from "./ConnectMatrixChannelAction";
import { enMessagingMatrixMessages } from "./i18n";

const messagingIntegrateMatrix = defineChannelBridgeAddon({
  id: "messaging-integrate-matrix",
  key: "matrix",
  sequence: 23,
  connectAction: <ConnectMatrixChannelAction />,
  i18n: { messaging: enMessagingMatrixMessages },
  instructionKey: "channel.matrix.recovery",
});

export default messagingIntegrateMatrix;

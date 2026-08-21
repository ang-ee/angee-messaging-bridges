import {
  ConnectChannelAction,
  type ConnectChannelFields,
} from "@angee/messaging";
import type { AuthoredVariables } from "@angee/refine";
import {
  mutationDialogValueCodecs,
  type MutationDialogValues,
} from "@angee/ui";
import * as React from "react";

import { ConnectWhatsappChannel } from "./documents";

const fields: ConnectChannelFields = (t) => [
  {
    name: "name",
    label: t("channel.connect.name"),
    placeholder: t("channel.whatsapp.namePlaceholder"),
    required: true,
  },
];

function parseValues(
  values: MutationDialogValues,
): AuthoredVariables<typeof ConnectWhatsappChannel> {
  return {
    name: mutationDialogValueCodecs.requiredString(values.name, "name"),
  };
}

/** WhatsApp field declaration followed by the shared pairing hand-off. */
export function ConnectWhatsappChannelAction(): React.ReactElement {
  return (
    <ConnectChannelAction
      kind="pairing"
      document={ConnectWhatsappChannel}
      fields={fields}
      i18nPrefix="channel.whatsapp"
      parseValues={parseValues}
      resultField="connect_whatsapp_channel"
      instructionKey="channel.whatsapp.scan"
    />
  );
}

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

import { ConnectMatrixChannel } from "./documents";

const fields: ConnectChannelFields = (t) => [
  {
    name: "homeserver",
    label: t("channel.matrix.homeserver"),
    placeholder: t("channel.matrix.homeserverPlaceholder"),
    required: true,
  },
  {
    name: "username",
    label: t("channel.matrix.username"),
    placeholder: t("channel.matrix.usernamePlaceholder"),
    required: true,
  },
  {
    name: "password",
    label: t("channel.matrix.password"),
    widget: "password",
    required: true,
  },
];

function parseValues(
  values: MutationDialogValues,
): AuthoredVariables<typeof ConnectMatrixChannel> {
  return {
    homeserver: mutationDialogValueCodecs.requiredString(
      values.homeserver,
      "homeserver",
    ),
    username: mutationDialogValueCodecs.requiredString(values.username, "username"),
    password: mutationDialogValueCodecs.verbatimString(values.password, "password"),
  };
}

/** Matrix field declaration followed by the shared pairing hand-off. */
export function ConnectMatrixChannelAction(): React.ReactElement {
  return (
    <ConnectChannelAction
      kind="pairing"
      document={ConnectMatrixChannel}
      fields={fields}
      i18nPrefix="channel.matrix"
      parseValues={parseValues}
      resultField="connect_matrix_channel"
      instructionKey="channel.matrix.recovery"
    />
  );
}

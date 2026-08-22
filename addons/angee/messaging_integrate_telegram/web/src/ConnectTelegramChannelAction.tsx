import {
  ConnectChannelAction,
  type ConnectChannelFields,
} from "@angee/messaging";
import {
  useAuthoredMutation,
  type AuthoredVariables,
} from "@angee/refine";
import {
  mutationDialogValueCodecs,
  type MutationDialogField,
  type MutationDialogValues,
} from "@angee/ui";
import * as React from "react";

import { ConnectTelegramChannel, CreateTelegramAppKeys } from "./documents";

const APP_KEYS_ONLY = [
  { field: "kind", operator: "eq", value: "app_keys" },
] as const;

function parseValues(
  values: MutationDialogValues,
): AuthoredVariables<typeof ConnectTelegramChannel> {
  return {
    name: mutationDialogValueCodecs.requiredString(values.name, "name"),
    credentialId: mutationDialogValueCodecs.requiredString(
      values.credential,
      "credential",
    ),
  };
}

/** Telegram declarations plus its credential-creation relation affordance. */
export function ConnectTelegramChannelAction(): React.ReactElement {
  const [createAppKeys] = useAuthoredMutation(CreateTelegramAppKeys);
  const fields = React.useCallback<ConnectChannelFields>(
    (translate) => {
      const createFields: readonly MutationDialogField[] = [
        {
          name: "name",
          label: translate("channel.connect.name"),
          placeholder: translate("channel.telegram.keysNamePlaceholder"),
          required: true,
        },
        {
          name: "app_id",
          label: translate("channel.telegram.apiId"),
          kind: "integer",
          placeholder: translate("channel.telegram.apiIdPlaceholder"),
          required: true,
        },
        {
          name: "app_secret",
          label: translate("channel.telegram.apiHash"),
          widget: "password",
          placeholder: translate("channel.telegram.apiHashPlaceholder"),
          required: true,
          description: (
            <>
              <span>{translate("channel.telegram.keysHelp")}</span>
              <br />
              <a href="https://my.telegram.org/" target="_blank" rel="noreferrer">
                {translate("channel.telegram.keysLink")}
              </a>
            </>
          ),
        },
      ];
      return [
        {
          name: "name",
          label: translate("channel.connect.name"),
          placeholder: translate("channel.telegram.namePlaceholder"),
          required: true,
        },
        {
          name: "credential",
          label: translate("channel.telegram.credential"),
          placeholder: translate("channel.telegram.credentialPlaceholder"),
          required: true,
          description: translate("channel.telegram.credentialHelp"),
          relation: {
            resource: "integrate.Credential",
            filters: APP_KEYS_ONLY,
            create: {
              resource: "integrate.Credential",
              fields: createFields,
              title: translate("channel.telegram.keysTitle"),
              submit: async (values) => {
                const appId = mutationDialogValueCodecs.integer(
                  values.app_id,
                  translate("channel.telegram.apiId"),
                  (label) =>
                    translate("channel.telegram.apiIdInvalid", { label }),
                );
                if (appId === null) {
                  throw new TypeError(
                    'Telegram app-key invariant: required field "app_id" was empty.',
                  );
                }
                const created = await createAppKeys({
                  name: mutationDialogValueCodecs.requiredString(
                    values.name,
                    "name",
                  ),
                  appId: String(appId),
                  appSecret: mutationDialogValueCodecs.requiredString(
                    values.app_secret,
                    "app_secret",
                  ),
                });
                return created?.create_credential ?? null;
              },
            },
          },
        },
      ];
    },
    [createAppKeys],
  );

  return (
    <ConnectChannelAction
      kind="pairing"
      document={ConnectTelegramChannel}
      fields={fields}
      i18nPrefix="channel.telegram"
      parseValues={parseValues}
      resultField="connect_telegram_channel"
      instructionKey="channel.telegram.scan"
    />
  );
}

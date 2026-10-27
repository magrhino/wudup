import {
  h,
  inject,
  provide,
  ref,
  watch,
  type Component,
  type Ref,
  type VNodeChild,
} from "vue";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import type { Router } from "vue-router";

type MountOptions = {
  pinia?: Pinia;
  router?: Router;
};

type DataTableColumn = {
  key?: string;
  type?: string;
  render?: (row: Record<string, unknown>) => VNodeChild;
};

type TabsState = {
  activeName: Ref<string | undefined>;
};

const passthrough = (tag: string) => ({
  setup(_: unknown, { slots }: { slots: Record<string, () => unknown> }) {
    return () => h(tag, [slots.default?.()]);
  },
});

const passthroughAttrs = (tag: string) => ({
  setup(
    _: unknown,
    {
      attrs,
      slots,
    }: { attrs: Record<string, unknown>; slots: Record<string, () => unknown> },
  ) {
    return () => h(tag, attrs, [slots.default?.()]);
  },
});

const tabsInjectionKey = Symbol("tabs");

function callUpdateValue(listener: unknown, value: string): void {
  if (typeof listener === "function") {
    listener(value);
  } else if (Array.isArray(listener)) {
    for (const handler of listener) {
      callUpdateValue(handler, value);
    }
  }
}

const nInputStub: Component = {
  props: {
    disabled: Boolean,
    inputProps: Object,
    placeholder: String,
    size: String,
    type: String,
    value: [String, Number],
  },
  emits: ["update:value"],
  setup(props, { attrs, emit }) {
    return () => {
      const { onUpdateValue, ...inputAttrs } = attrs as Record<string, unknown>;
      return h("input", {
        ...inputAttrs,
        ...(props.inputProps as Record<string, unknown> | undefined),
        disabled: props.disabled,
        placeholder: props.placeholder,
        type: props.type || "text",
        value: props.value ?? "",
        onInput: (event: Event) => {
          const value = (event.target as HTMLInputElement).value;
          emit("update:value", value);
          callUpdateValue(onUpdateValue, value);
        },
      });
    };
  },
};

export const naiveStubs: Record<string, Component> = {
  NAlert: {
    props: {
      type: String,
    },
    setup(props, { slots }) {
      return () =>
        h(
          "div",
          {
            role: props.type === "error" ? "alert" : "status",
            "data-alert-type": props.type,
          },
          [slots.default?.()],
        );
    },
  },
  NButton: {
    props: {
      attrType: String,
      disabled: Boolean,
      loading: Boolean,
      title: String,
      type: String,
    },
    emits: ["click"],
    setup(props, { attrs, emit, slots }) {
      return () =>
        h(
          "button",
          {
            ...attrs,
            type: props.attrType || "button",
            "data-button-type": props.type,
            disabled: props.disabled || props.loading,
            title: props.title,
            onClick: (event: MouseEvent) => emit("click", event),
          },
          [slots.icon?.(), slots.default?.()],
        );
    },
  },
  NCheckbox: {
    props: {
      checked: Boolean,
      disabled: Boolean,
      indeterminate: Boolean,
    },
    emits: ["update:checked"],
    setup(props, { attrs, emit, slots }) {
      return () =>
        h("label", [
          h("input", {
            ...attrs,
            type: "checkbox",
            checked: props.checked,
            disabled: props.disabled,
            indeterminate: props.indeterminate,
            "aria-checked": props.indeterminate ? "mixed" : String(props.checked),
            onChange: (event: Event) =>
              emit("update:checked", (event.target as HTMLInputElement).checked),
          }),
          slots.default?.(),
        ]);
    },
  },
  NConfigProvider: passthrough("div"),
  NDataTable: {
    props: {
      columns: Array,
      data: Array,
      checkedRowKeys: Array,
      rowKey: Function,
    },
    emits: ["update:checked-row-keys"],
    setup(props, { emit }) {
      return () =>
        h(
          "div",
          { role: "table" },
          (props.data ?? []).map((row: unknown) => {
            const record = row as Record<string, unknown> & {
              line_no: number;
              image: string;
            };
            const columns = (props.columns ?? []) as DataTableColumn[];
            const key = props.rowKey
              ? props.rowKey(record)
              : record.line_no;
            const checked = (props.checkedRowKeys ?? []).includes(key);
            return h("div", { role: "row", key }, [
              h("label", [
                h("input", {
                  type: "checkbox",
                  checked,
                  onChange: (event: Event) => {
                    const selected = new Set(props.checkedRowKeys ?? []);
                    if ((event.target as HTMLInputElement).checked) {
                      selected.add(key);
                    } else {
                      selected.delete(key);
                    }
                    emit("update:checked-row-keys", [...selected]);
                  },
                }),
                h("span", record.image),
              ]),
              ...columns
                .filter((column) => column.type !== "selection")
                .map((column, index) =>
                  h(
                    "span",
                    { role: "cell", key: column.key ?? index },
                    column.render
                      ? [column.render(record)]
                      : [String(column.key ? record[column.key] ?? "" : "")],
                  ),
                ),
            ]);
          }),
        );
    },
  },
  NDropdown: {
    props: {
      disabled: Boolean,
      options: { type: Array, default: () => [] },
      renderLabel: Function,
    },
    emits: ["select"],
    setup(props, { emit, slots }) {
      type Option = {
        disabled?: boolean;
        icon?: () => VNodeChild;
        key: string;
        label: string;
      };
      const renderLabel = (option: Option): VNodeChild =>
        props.renderLabel ? (props.renderLabel as (option: Option) => VNodeChild)(option) : option.label;
      return () =>
        h("span", [
          slots.default?.(),
          h(
            "span",
            { role: "menu" },
            (props.options as Option[]).map((option) =>
              h(
                "button",
                {
                  type: "button",
                  role: "menuitem",
                  "data-key": option.key,
                  disabled: props.disabled || option.disabled,
                  onClick: () => emit("select", option.key, option),
                },
                [option.icon?.(), renderLabel(option)],
              ),
            ),
          ),
        ]);
    },
  },
  NEmpty: {
    props: {
      description: String,
      showIcon: Boolean,
    },
    setup(props, { attrs, slots }) {
      return () =>
        h("div", attrs, [
          slots.default?.() ?? props.description,
          slots.extra?.(),
        ]);
    },
  },
  NFlex: passthroughAttrs("div"),
  NForm: {
    emits: ["submit"],
    setup(_, { emit, slots }) {
      return () =>
        h(
          "form",
          {
            onSubmit: (event: Event) => {
              event.preventDefault();
              emit("submit", event);
            },
          },
          [slots.default?.()],
        );
    },
  },
  NFormItem: {
    props: {
      label: String,
      labelProps: Object,
    },
    setup(props, { slots }) {
      return () =>
        h("label", props.labelProps as Record<string, unknown> | undefined, [
          props.label ? h("span", props.label) : null,
          slots.default?.(),
        ]);
    },
  },
  NGi: passthroughAttrs("div"),
  NGrid: passthroughAttrs("div"),
  NInput: nInputStub,
  Input: nInputStub,
  NInputNumber: {
    props: {
      disabled: Boolean,
      value: Number,
    },
    emits: ["update:value"],
    setup(props, { emit }) {
      return () =>
        h("input", {
          disabled: props.disabled,
          type: "number",
          value: props.value ?? "",
          onInput: (event: Event) => {
            const value = (event.target as HTMLInputElement).value;
            emit("update:value", value ? Number(value) : null);
          },
        });
    },
  },
  NMessageProvider: passthrough("div"),
  NModal: {
    props: {
      positiveButtonProps: Object,
      positiveText: String,
      show: Boolean,
      title: String,
    },
    emits: ["positive-click", "update:show"],
    setup(props, { emit, slots }) {
      return () => {
        const positiveButtonProps = props.positiveButtonProps as
          | { disabled?: boolean; loading?: boolean }
          | undefined;
        const positiveDisabled = Boolean(
          positiveButtonProps?.disabled || positiveButtonProps?.loading,
        );
        return props.show
          ? h("div", { role: "dialog" }, [
              props.title ? h("h2", props.title) : null,
              slots.default?.(),
              props.positiveText
                ? h(
                    "button",
                    {
                      disabled: positiveDisabled,
                      onClick: positiveDisabled
                        ? undefined
                        : () => emit("positive-click"),
                    },
                    props.positiveText,
                  )
                : null,
            ])
          : null;
      };
    },
  },
  NPagination: {
    props: {
      page: Number,
      pageCount: Number,
    },
    emits: ["update:page"],
    setup(props, { emit }) {
      return () =>
        h(
          "nav",
          { "aria-label": "Pagination" },
          Array.from({ length: props.pageCount ?? 0 }, (_, index) => {
            const page = index + 1;
            return h(
              "button",
              {
                disabled: page === props.page,
                type: "button",
                onClick: () => emit("update:page", page),
              },
              String(page),
            );
          }),
        );
    },
  },
  NRadioButton: {
    props: {
      disabled: Boolean,
      title: String,
      value: String,
    },
    setup(props, { slots }) {
      const group = inject<{
        value: Ref<string | undefined>;
        update: (value: string) => void;
      } | null>("n-radio-group", null);
      return () =>
        h("label", [
          h("input", {
            type: "radio",
            checked: group?.value.value === props.value,
            disabled: props.disabled,
            title: props.title,
            value: props.value,
            onChange: () => {
              if (props.value) {
                group?.update(props.value);
              }
            },
          }),
          slots.default?.(),
        ]);
    },
  },
  NRadioGroup: {
    props: {
      value: String,
    },
    emits: ["update:value"],
    setup(props, { attrs, emit, slots }) {
      const value = ref(props.value);
      watch(
        () => props.value,
        (next) => {
          value.value = next;
        },
      );
      provide("n-radio-group", {
        value,
        update: (next: string) => {
          value.value = next;
          emit("update:value", next);
          callUpdateValue(
            (attrs as Record<string, unknown>).onUpdateValue,
            next,
          );
        },
      });
      return () => h("div", attrs, [slots.default?.()]);
    },
  },
  NSelect: {
    props: {
      disabled: Boolean,
      multiple: Boolean,
      options: Array,
      value: [String, Number, Array],
    },
    emits: ["update:value"],
    setup(props, { attrs, emit }) {
      return () =>
        h(
          "select",
          {
            ...attrs,
            disabled: props.disabled,
            multiple: props.multiple,
            value: props.value ?? (props.multiple ? [] : ""),
            onChange: (event: Event) => {
              const select = event.target as HTMLSelectElement;
              emit(
                "update:value",
                props.multiple
                  ? Array.from(select.selectedOptions).map((option) => option.value)
                  : select.value,
              );
            },
          },
          (props.options as { label: string; value: string | number }[] | undefined)?.map(
            (option) =>
              h("option", { key: option.value, value: option.value }, option.label),
          ),
        );
    },
  },
  NSkeleton: {
    setup(_, { attrs }) {
      return () => h("div", { ...attrs, "aria-hidden": "true" });
    },
  },
  NSwitch: {
    props: {
      disabled: Boolean,
      value: Boolean,
    },
    emits: ["update:value"],
    setup(props, { emit }) {
      return () =>
        h("input", {
          role: "switch",
          type: "checkbox",
          checked: props.value,
          disabled: props.disabled,
          onChange: (event: Event) =>
            emit("update:value", (event.target as HTMLInputElement).checked),
        });
    },
  },
  NTabPane: {
    props: {
      name: String,
      tab: String,
    },
    setup(props, { slots }) {
      const tabsState = inject<TabsState | null>(tabsInjectionKey, null);
      if (tabsState && !tabsState.activeName.value) {
        tabsState.activeName.value = props.name;
      }
      return () =>
        !tabsState || tabsState.activeName.value === props.name
          ? h(
              "div",
              {
                role: "tabpanel",
                "data-tab-pane": props.name,
              },
              [slots.default?.()],
            )
          : null;
    },
  },
  NTabs: {
    setup(_, { slots }) {
      const tabsState: TabsState = { activeName: ref() };
      provide(tabsInjectionKey, tabsState);
      return () => {
        const panes = slots.default?.() ?? [];
        return h("div", [
          h(
            "div",
            { role: "tablist" },
            panes.map((pane) => {
              const name = String(pane.props?.name ?? "");
              return h(
                "button",
                {
                  role: "tab",
                  type: "button",
                  "aria-selected": String(tabsState.activeName.value === name),
                  onClick: () => {
                    tabsState.activeName.value = name;
                  },
                },
                String(pane.props?.tab ?? name),
              );
            }),
          ),
          panes,
        ]);
      };
    },
  },
  NTag: passthrough("span"),
  NTooltip: {
    setup(_, { slots }) {
      return () => h("span", [slots.trigger?.(), slots.default?.()]);
    },
  },
};

Object.assign(naiveStubs, {
  Alert: naiveStubs.NAlert,
  Button: naiveStubs.NButton,
  Checkbox: naiveStubs.NCheckbox,
  ConfigProvider: naiveStubs.NConfigProvider,
  DataTable: naiveStubs.NDataTable,
  Dropdown: naiveStubs.NDropdown,
  Empty: naiveStubs.NEmpty,
  Flex: naiveStubs.NFlex,
  Form: naiveStubs.NForm,
  FormItem: naiveStubs.NFormItem,
  Gi: naiveStubs.NGi,
  Grid: naiveStubs.NGrid,
  GridItem: naiveStubs.NGi,
  Input: naiveStubs.NInput,
  InputNumber: naiveStubs.NInputNumber,
  MessageProvider: naiveStubs.NMessageProvider,
  Modal: naiveStubs.NModal,
  NGridItem: naiveStubs.NGi,
  Pagination: naiveStubs.NPagination,
  RadioButton: naiveStubs.NRadioButton,
  RadioGroup: naiveStubs.NRadioGroup,
  Select: naiveStubs.NSelect,
  Skeleton: naiveStubs.NSkeleton,
  Switch: naiveStubs.NSwitch,
  TabPane: naiveStubs.NTabPane,
  Tabs: naiveStubs.NTabs,
  Tag: naiveStubs.NTag,
  Tooltip: naiveStubs.NTooltip,
});

export function mountWithApp(component: Component, options: MountOptions = {}): VueWrapper {
  const pinia = options.pinia ?? createPinia();
  setActivePinia(pinia);

  const plugins = options.router ? [pinia, options.router] : [pinia];
  return mount(component, {
    global: {
      plugins,
      stubs: {
        ...naiveStubs,
        RouterLink: {
          props: { to: [String, Object] },
          setup(props, { attrs, slots }) {
            return () =>
              h(
                "a",
                {
                  ...attrs,
                  href: typeof props.to === "string" ? props.to : "#",
                },
                [slots.default?.()],
              );
          },
        },
      },
    },
  });
}

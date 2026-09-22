import {
  createRouter,
  createWebHashHistory,
  type RouterHistory,
  type RouteRecordRaw,
} from "vue-router";

import { useAuthStore } from "../stores/auth";

export const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("../views/LoginView.vue"),
    meta: { requiresAuth: false },
  },
  {
    path: "/setup",
    name: "setup",
    component: () => import("../views/SetupView.vue"),
    meta: { requiresAuth: false },
  },
  {
    path: "/reset-admin",
    name: "reset-admin",
    component: () => import("../views/ResetAdminView.vue"),
    meta: { requiresAuth: false },
  },
  {
    path: "/",
    name: "dashboard",
    component: () => import("../views/DashboardView.vue"),
    meta: { requiresAuth: true, title: "Dashboard" },
  },
  {
    path: "/pending",
    name: "pending",
    component: () => import("../views/PendingView.vue"),
    meta: { requiresAuth: true, title: "Pending updates" },
  },
  {
    path: "/retags",
    name: "retags",
    component: () => import("../views/RetagsView.vue"),
    meta: { requiresAuth: true, title: "Retags" },
  },
  {
    path: "/containers",
    name: "containers",
    component: () => import("../views/TrackedContainersView.vue"),
    meta: { requiresAuth: true, title: "Tracked containers" },
  },
  {
    path: "/runs",
    name: "runs",
    component: () => import("../views/RunsView.vue"),
    meta: { requiresAuth: true, title: "History" },
  },
  {
    path: "/audit",
    name: "audit",
    component: () => import("../views/AuditView.vue"),
    meta: { requiresAuth: true, title: "History" },
  },
  {
    path: "/policies",
    name: "policies",
    component: () => import("../views/PoliciesView.vue"),
    meta: { requiresAuth: true, title: "Service policies" },
  },
  {
    path: "/snoozes",
    name: "snoozes",
    component: () => import("../views/SnoozesView.vue"),
    meta: { requiresAuth: true, title: "Snoozes" },
  },
  {
    path: "/tag-exclusions",
    name: "tag-exclusions",
    component: () => import("../views/TagExclusionsView.vue"),
    meta: { requiresAuth: true, title: "Tag exclusions" },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("../views/SettingsView.vue"),
    meta: { requiresAuth: true, title: "Settings" },
  },
  {
    path: "/doctor",
    name: "doctor",
    component: () => import("../views/DoctorView.vue"),
    meta: { requiresAuth: true, title: "Doctor" },
  },
  {
    path: "/diagnostics/issue-dump",
    name: "issue-dump",
    component: () => import("../views/IssueDumpView.vue"),
    meta: { requiresAuth: true, title: "Affected containers" },
  },
  {
    path: "/runs/:id",
    name: "run-detail",
    component: () => import("../views/RunDetailView.vue"),
    meta: { requiresAuth: true, title: "Run detail" },
  },
  {
    path: "/runs/:id/log",
    name: "run-log",
    component: () => import("../views/LogView.vue"),
    meta: { requiresAuth: true, title: "Log viewer" },
  },
  {
    path: "/:pathMatch(.*)*",
    redirect: "/",
  },
];

export function createWudRouter(history: RouterHistory = createWebHashHistory()) {
  const router = createRouter({ history, routes });

  router.beforeEach(async (to) => {
    const auth = useAuthStore();
    if (auth.session === null && !auth.loading) {
      await auth.loadSession();
    }
    if (auth.setupRequired && to.name !== "setup") {
      return { name: "setup", query: to.query };
    }
    if (to.name === "setup" && !auth.setupRequired) {
      return auth.authenticated ? { name: "dashboard" } : { name: "login" };
    }
    if (to.meta.requiresAuth !== false && !auth.authenticated) {
      return { name: "login", query: { redirect: to.fullPath } };
    }
    if (to.name === "login" && auth.authenticated) {
      return { name: "dashboard" };
    }
    return true;
  });

  return router;
}

export const router = createWudRouter();

import { api } from "@/lib/api/client";
import type { UserProfile } from "@/types/api";

export function register(email: string, password: string, displayName: string) {
  return api<UserProfile>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, displayName }),
  });
}

export function login(email: string, password: string) {
  return api<UserProfile>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function logout() {
  return api<undefined>("/api/auth/logout", { method: "POST" });
}

export function me() {
  return api<UserProfile>("/api/auth/me");
}

import { api } from "@/lib/api/client";

export interface Profile {
  displayName: string;
  studyLevel: number;
  preferredLanguage: string;
  externalAiAllowed: boolean;
}

export function getProfile(): Promise<Profile> {
  return api<Profile>("/api/profile");
}

export function updateProfile(patch: Partial<Profile>): Promise<Profile> {
  return api<Profile>("/api/profile", { method: "PATCH", body: JSON.stringify(patch) });
}

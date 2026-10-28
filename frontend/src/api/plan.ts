import { api, ApiError } from './client';
import { mockEnabled } from './screens';
import { setupGet } from './setup';
import { copy } from '../copy';

export interface PlanAsset { id: number; url: string; kind: 'image' | 'upload'; alt: string; label: string; selected: boolean }
export interface PlanGroup {
  id: number; name: string; platform: 'direct' | 'vk'; audience: string; daily_limit_kop: number;
  ads: { title: string; text: string }[]; assets: PlanAsset[];
}
export interface Plan {
  id: number; month_budget_kop: number; direct_budget_kop: number; vk_budget_kop: number;
  groups: PlanGroup[]; generations_remaining: number; medical_level: 'none' | 'maybe' | 'required';
  marking_confirmed: boolean; ready_to_launch: boolean; launch_error?: string | null; status: 'draft' | 'launched';
}
export async function getPlan(signal?: AbortSignal): Promise<Plan> {
  if (mockEnabled) return (await import('./mocks')).mockSetupPlan();
  return setupGet('/api/plan', signal);
}
export async function regenerateImages(group_id: number): Promise<Plan> {
  if (mockEnabled) return (await import('./mocks')).mockPlanRegenerate(group_id);
  const result = await api<Plan>('/api/plan/regenerate-images', { method: 'POST', body: { group_id } });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function uploadPhoto(group_id: number, file: File): Promise<Plan> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024 || !file.size) {
    throw new ApiError(copy.launchFlow.photoError);
  }
  if (mockEnabled) return (await import('./mocks')).mockPlanUpload(group_id, file);
  const body = new FormData(); body.append('group_id', String(group_id)); body.append('file', file);
  try {
    const result = await api<Plan>('/api/plan/upload-photo', { method: 'POST', body });
    if (!result) throw new ApiError(copy.errors.invalidResponse);
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.status >= 400 && error.status !== 401) {
      throw new ApiError(error.detail ?? copy.launchFlow.uploadError, error.status, error.detail);
    }
    throw error;
  }
}
export async function saveAssets(asset_ids: number[]) {
  if (mockEnabled) { (await import('./mocks')).mockPlanAssets(asset_ids); return; }
  await api('/api/plan/assets', { method: 'PUT', body: { asset_ids } });
}
export async function launchPlan(plan_id: number, medical_acknowledged: boolean) {
  if (mockEnabled) { (await import('./mocks')).mockPlanLaunch(medical_acknowledged); return; }
  await api('/api/plan/launch', { method: 'POST', body: { plan_id, medical_acknowledged } });
}

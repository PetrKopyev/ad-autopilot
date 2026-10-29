import { copy } from '../copy';

type Platform = keyof typeof copy.screens.platforms;

/** Площадка: цветная точка (Директ — красная, ВК — синяя) и название. */
export function PlatformBadge({ platform }: { platform: string }) {
  const name = copy.screens.platforms[platform as Platform] ?? platform;
  return <span className={`platform-badge platform-badge--${platform}`}><span className="platform-badge__dot" aria-hidden="true" />{name}</span>;
}

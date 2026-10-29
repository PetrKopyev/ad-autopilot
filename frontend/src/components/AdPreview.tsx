import { copy } from '../copy';
import './AdPreview.css';

export interface AdPreviewProps {
  platform: 'direct' | 'vk';
  title: string;
  text: string;
  /** Адрес сайта из настроек бизнеса; пока его нет — «Ваш сайт». */
  site?: string;
  image?: { url: string; alt: string };
}

function domain(site?: string) {
  try { return site ? new URL(site).hostname.replace(/^www\./, '') : copy.plan.previewSite; } catch { return site ?? copy.plan.previewSite; }
}

/** Примерный вид объявления: Директ — строка поисковой выдачи Яндекса, ВКонтакте — карточка ленты. Только то, что
 *  уходит на площадку: заголовок, текст, адрес сайта, картинка; рейтинги, телефоны и быстрые ссылки не выдумываем. */
export function AdPreview({ platform, title, text, site, image }: AdPreviewProps) {
  const host = domain(site);
  return <figure className="ad-preview">
    {platform === 'direct' ? <div className="ad-preview__serp">
      <p className="ad-preview__label">{copy.plan.previewAd} · {host}</p>
      <p className="ad-preview__title">{title}</p>
      <p className="ad-preview__url">{host}</p>
      <p className="ad-preview__text">{text}</p>
    </div> : <div className="ad-preview__feed">
      <div className="ad-preview__author"><span className="ad-preview__avatar" aria-hidden="true">{host.charAt(0).toUpperCase()}</span>
        <span><b>{host}</b><small>{copy.plan.previewAd}</small></span></div>
      <p className="ad-preview__text">{text}</p>
      {image ? <img className="ad-preview__image" src={image.url} alt={image.alt} /> : <p className="ad-preview__no-image">{copy.plan.previewNoImage}</p>}
      <div className="ad-preview__link"><b>{title}</b><small>{host}</small></div>
    </div>}
    <figcaption>{copy.plan.previewNote}</figcaption>
  </figure>;
}

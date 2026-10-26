import { copy } from '../copy';
import { Button } from './Button';

export interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
}

export function ErrorBanner({ message, onRetry, onDismiss }: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <p>{message}</p>
      {(onRetry || onDismiss) && <div className="actions">
        {onRetry && <Button variant="secondary" onClick={onRetry}>{copy.common.retry}</Button>}
        {onDismiss && <Button variant="secondary" onClick={onDismiss}>{copy.common.close}</Button>}
      </div>}
    </div>
  );
}

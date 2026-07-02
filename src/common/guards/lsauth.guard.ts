import { Injectable, CanActivate } from '@nestjs/common';

/**
 * LsAuthGuard — placeholder guard for this spoke service.
 *
 * Authentication is enforced at the API Gateway layer via a Lambda Authorizer
 * checking the Authorization header against Secrets Manager. Requests that
 * reach this Lambda have already been validated by the authorizer, so this
 * guard always returns true.
 *
 * In e2e tests, this guard is overridden by MockGuard (test/mock-guard.ts).
 */
@Injectable()
export class LsAuthGuard implements CanActivate {
    canActivate(): boolean {
        return true;
    }
}

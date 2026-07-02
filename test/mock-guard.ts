import { CanActivate } from '@nestjs/common';

export class MockGuard implements CanActivate {
    canActivate(): boolean {
        return true;
    }
}

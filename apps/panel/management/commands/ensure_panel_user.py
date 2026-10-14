from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "确保网页控制台可用：无用户时创建 admin / admin123；可 --reset 强制重置密码"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="将 admin 密码重置为 admin123（不存在则创建）",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        reset = bool(options.get("reset"))
        if reset:
            user, created = User.objects.get_or_create(
                username="admin",
                defaults={
                    "email": "admin@localhost",
                    "is_staff": True,
                    "is_superuser": True,
                },
            )
            user.is_staff = True
            user.is_superuser = True
            user.set_password("admin123")
            user.save()
            action = "已创建" if created else "已重置"
            self.stdout.write(
                self.style.SUCCESS(f"{action}超级用户 admin / admin123")
            )
            return
        if User.objects.exists():
            self.stdout.write(
                self.style.SUCCESS("已有用户，跳过创建（需要重置请加 --reset）")
            )
            return
        User.objects.create_superuser("admin", "admin@localhost", "admin123")
        self.stdout.write(
            self.style.SUCCESS("已创建超级用户 admin / admin123（请尽快修改密码）")
        )

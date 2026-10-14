from django.db import migrations, models


def forwards_copy_remark(apps, schema_editor):
    Keyword = apps.get_model("rules", "Keyword")
    for obj in Keyword.objects.all():
        if (obj.usages or "").strip():
            continue
        remark = (obj.remark or "").strip()
        if not remark:
            continue
        kw = (obj.keyword or "").strip()
        lines = [remark]
        # 充币类：补全「无空格金额 / 查询正反序」常用写法
        if kw and remark.replace(" ", "").startswith(kw):
            glued = remark.replace(" ", "")
            if glued != remark and glued not in lines:
                lines.insert(0, glued)
            spaced = f"{kw} {remark[len(kw) :].lstrip()}" if remark.startswith(kw) else ""
            if spaced and spaced not in lines and spaced != remark:
                lines.append(spaced)
            for q in (f"查询{kw}", f"{kw}查询"):
                if q not in lines:
                    lines.append(q)
        obj.usages = "\n".join(lines)
        obj.save(update_fields=["usages"])


class Migration(migrations.Migration):

    dependencies = [
        ("rules", "0003_book_room_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="keyword",
            name="usages",
            field=models.TextField(blank=True, default="", verbose_name="用法列表"),
        ),
        migrations.RunPython(forwards_copy_remark, migrations.RunPython.noop),
    ]

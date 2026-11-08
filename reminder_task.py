# reminder_task.py
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import timedelta

# 从你的 app.py 导入必须的对象和函数
from app import app, db, Assignment, User, Submission, get_bj_time

def send_reminder_email(to_email, student_name, assignment):
    mail_user = os.environ.get('GMAIL_USER')
    mail_pass = os.environ.get('GMAIL_PASS')
    if not mail_user or not mail_pass:
        print("未配置发信邮箱环境变量，跳过发送")
        return

    try:
        subject = f"【作业提醒】《{assignment.title}》即将截止"
        body = f"您好，{student_name}：\n\n系统提醒您，作业《{assignment.title}》即将于 {assignment.deadline.strftime('%Y-%m-%d %H:%M')} 截止。\n\n请您抓紧时间登录系统完成提交。\n\n祝您学习顺利！"
        message = MIMEText(body, 'plain', 'utf-8')
        message['From'] = Header(f"作业提交系统 <{mail_user}>")
        message['To'] = Header(to_email)
        message['Subject'] = Header(subject)

        smtpObj = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10)
        smtpObj.login(mail_user, mail_pass)
        smtpObj.sendmail(mail_user, [to_email], message.as_string())
        smtpObj.quit()
        print(f"[{get_bj_time()}] 成功发送提醒给 {student_name} ({to_email})")
    except Exception as e:
        print(f"[{get_bj_time()}] 发送提醒邮件给 {to_email} 失败: {e}")

def run_check():
    with app.app_context():
        now = get_bj_time()
        # 提前 6 小时
        target_time = now + timedelta(hours=6)
        
        assignments = Assignment.query.filter(
            Assignment.deadline <= target_time,
            Assignment.deadline > now,
            Assignment.reminder_sent == False,
            Assignment.is_forced_closed == False,
            Assignment.is_active == False
        ).all()

        if not assignments:
            print(f"[{get_bj_time()}] 没有需要发送提醒的作业。")
            return

        for a in assignments:
            # 数据库原子锁：防止重复发信
            result = db.session.execute(
                db.text("UPDATE assignment SET reminder_sent = 1 WHERE id = :id AND reminder_sent = 0"),
                {"id": a.id}
            )
            db.session.commit()
            
            if result.rowcount > 0:
                all_students = User.query.filter(User.role.in_(['student', 'admin']), User.username != 'admin').all()
                if a.class_name == '全部班级':
                    target_students = all_students
                else:
                    target_students = [u for u in all_students if u.class_name == a.class_name]
                    
                subs = Submission.query.filter_by(assignment_id=a.id).all()
                sub_ids = [s.student_id for s in subs]
                not_submitted = [u for u in target_students if u.username not in sub_ids and u.email]
                
                print(f"[{get_bj_time()}] 准备为作业《{a.title}》发送 {len(not_submitted)} 封提醒邮件...")
                for stu in not_submitted:
                    send_reminder_email(stu.email, stu.name, a)

if __name__ == '__main__':
    run_check()
    print(f"[{get_bj_time()}] 检查任务执行完毕。")
from WindPy import w

w.start() # 默认命令超时时间为120秒，如需设置超时时间可以加入waitTime参数，例如waitTime=60,即设置命令超时时间为60秒

w.isconnected() # 判断WindPy是否已经登录成功
# 可以使用如下命令停止WindPy：

w.stop() # 当需要停止WindPy时，可以使用该命令
          # 注： w.start不重复启动，若需要改变参数，如超时时间，用户可以使用w.stop命令先停止后再启动。
          # 退出时，会自动执行w.stop()，一般用户并不需要执行w.stop()，另外重复调用start/stop函数效果同单次调用。
# 需要注意的是，程序退出时会自动执行w.stop()，因此一般用户并不需要执行w.stop()

# 用户可以用w.menu()，显示导航界面，帮助创建命令。
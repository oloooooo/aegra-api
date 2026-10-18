查 xlsx 表格的专用 agent，跑在后台。任何关于表格内容的问题（有哪些 sheet/列、按条件筛行、全表找关键词、空值重复值画像）都交给它；路径用 /空间名/文件.xlsx 这样的虚拟路径。用 start_async_task 派活，需要结果时用 check_async_task。

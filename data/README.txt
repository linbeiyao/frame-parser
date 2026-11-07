frame-parser 样本数据目录
============================================================

【协议样本】由内置预设的构帧代码现场生成，校验一定对得上，
  在界面里「文件 → 样本数据」可直接打开：

  aa55_crc16.bin                   39 字节   ① 帧头 AA55 + 长度 + CRC16/MODBUS
  fixed_xor8.bin                   20 字节   ② 定长帧 10 字节 + XOR8
  modbus_rtu.bin                    8 字节   ③ Modbus RTU 读保持寄存器
  tlv.bin                          27 字节   ④ TLV 帧 (Tag 1B + Len 1B + Value)
  ascii_line.txt                   75 字节   ⑤ ASCII 行 ($POS…, \r\n 分隔)
  position_float.bin               52 字节   ⑥ 定位数据 (float64 经纬度 + 表达式)
  raw_packet.bin                   20 字节   ⑦ 整包查看（不拆分，手工核对字节）
  backward_eb90.bin                 0 字节   ⑧ 现场采集样本 backward_sample.txt (帧头 EB 90, 定长 156)
  can_sample.jsonl                120 行     仿 CAN 抓包 JSONL（可用行过滤 can_id 只看某一帧）
  can_sample.csv                  120 行     仿 CAN 分析仪 CSV（x| 数据列）

【日志样本】用来验证「从日志提取数据帧」（正则 + 行过滤）：
  · can_sample.jsonl —— 规则选「JSONL/JSON 的 data 字段」，
    行过滤填 0x1E7 就只看这个 CAN ID 的报文；
  · can_sample.csv  —— 规则选「CAN 分析仪 CSV 数据列」。

【现场采集样本】backward_sample.txt（预设 ⑧）：
  12663 帧 × 156 字节，帧头 EB 90、字节2 = 总长-5、字节5 = 报文类型 0x92、
  字节7 = 帧计数(每帧+2)、字节65/73/81/89 各 8 字节通道数据、帧尾 5B BF。
  用「定长帧 156」分帧即可；文件第一行注释会被自动跳过。

【切换样本目录】文件 → 样本数据 → 选择样本目录…（例如指到自己的日志目录）

【重新生成】python make_samples.py

当前目录里的日志/现场样本：
  backward_sample.txt        5926350 字节
  can_sample.jsonl              8298 字节
  can_sample.csv               11383 字节
